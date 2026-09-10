#!/usr/bin/env python3
"""herdlet - tiny coordination bus for coding agents in tmux panes.

tmux stays the multiplexer; herdlet adds the layer tmux doesn't have:

  - semantic agent state (idle / working / blocked / done)
  - push events (subscribe / wait), no capture-pane polling
  - a registry, so agents address each other by name instead of pane id

Transport is newline-delimited JSON over a unix socket, the same shape as
herdr's socket API, which herdlet deliberately mimics at a fraction of the
size. Pane I/O (send text, read scrollback) is delegated to tmux itself.
"""

import argparse
import asyncio
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

__version__ = "0.9.0"


def _log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


DEFAULT_SOCK = os.environ.get("HERDLET_SOCKET", os.path.expanduser("~/.herdlet.sock"))
LOG_PATH = os.path.expanduser("~/.herdlet.log")
STATES = ("idle", "working", "spawning", "blocked", "limited", "done", "ended", "unknown")
TERMINAL = ("done", "ended")  # agent's turn/session finished; record kept for collection + resume
MERGE_KEYS = ("message", "agent", "pane", "cwd", "session", "transcript", "model", "effort")
SHELLS = {"bash", "zsh", "fish", "sh", "dash", "ksh", "tcsh", "nu"}
# A pane sitting at a shell only means the agent DIED if it also stopped
# reporting. A live agent (wrapper script, `claude -p` piped to tee, a shell
# tool call) can legitimately show a shell as pane_current_command while its
# hooks keep the record fresh, so only call it stale once the record goes quiet.
STALE_AFTER = 60.0
# Terminal records older than this are pruned on daemon load so a long-lived
# socket doesn't accumulate dead agents forever.
TERMINAL_TTL = 86400.0
# A blocked agent's state is emitted once, on the hook that fired it - the
# harness has no "still waiting" event. So the daemon re-announces `blocked`
# to waiters every BLOCKED_REEMIT seconds, so a `wait` (especially `--edge`)
# that STARTED after the agent was already blocked still wakes instead of
# starving. Waiters only, not subscribers (`watch` stays a pure change stream;
# `monitor` already re-polls on its own tick). 0 disables.
BLOCKED_REEMIT = float(os.environ.get("HERDLET_BLOCKED_REEMIT", "30"))
# Registry GC: a record untouched for MAX_AGE is dropped regardless of state.
# A days-dead pane never reports a terminal state, so the terminal-only TTL
# above never catches it; the age cap does, and a still-live agent simply
# re-registers on its next hook. The daemon sweeps every PRUNE_INTERVAL seconds
# and also prunes on load. 0 disables the cap / the sweep respectively.
MAX_AGE = float(os.environ.get("HERDLET_MAX_AGE", str(3 * 86400)))
PRUNE_INTERVAL = float(os.environ.get("HERDLET_PRUNE_INTERVAL", "3600"))
# An agent parked on a usage-limit banner fires no hook, so the daemon scrapes
# its pane and reports `limited` instead. This covers Claude Code 2.1.263 and
# Codex 0.153 wording; fast-mode limits fall back to the normal one.
LIMIT_PATTERN_DEFAULT = (
    r"usage limit reached"
    r"|you.?ve (hit|reached) your\b(?! fast\b)"
    r"|you.?re out of (usage|extra usage)"
    r"|your org is out of usage"
    r"|your seat type doesn.?t include usage credits"
    r"|(session|weekly|monthly spend) limit reached"
    r"|your usage limit has reset"
    r"|when your limit resets"
)
LIMIT_INTERVAL = float(os.environ.get("HERDLET_LIMIT_INTERVAL", "30"))
LIMIT_SWEEP = os.environ.get("HERDLET_LIMIT_SWEEP", "1") not in ("0", "false", "no")
# Claude Code draws the banner just above the input box, so only the bottom of
# the VISIBLE pane can hold a real one; scrollback is old news at best.
LIMIT_TAIL = 8
LIMIT_WATCH = ("working", "spawning")  # `blocked` is already a wake signal
LIMIT_RE = None  # compiled by the daemon, its only consumer (see limit_regex)
RESUME = {
    "claude": "claude --resume {session}",
    "codex": "codex resume {session}",
    "opencode": "opencode --session {session}",
}


class Bus:
    def __init__(self, state_path=None):
        self.state_path = state_path
        self.agents = {}       # id -> record
        self.subscribers = set()  # (queue, id_filter, state_filter)
        self.waiters = []      # (predicate, future)
        self._reemit = {}      # id -> TimerHandle: live re-announce of `blocked`
        self._limit_timer = None
        self._limit_task = None
        self._load()

    def _load(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path) as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("agents"), dict):
                self.agents = data["agents"]
        except (OSError, json.JSONDecodeError):
            pass  # best-effort: a bad snapshot just means an empty registry
        # GC on load: drop cleanly-finished records after TERMINAL_TTL and any
        # record untouched past MAX_AGE (see _prunable), so a restart clears the
        # days-dead panes a terminal-only TTL would keep forever.
        now = time.time()
        kept = {aid: rec for aid, rec in self.agents.items()
                if not self._prunable(rec, now)}
        dropped = [aid for aid in self.agents if aid not in kept]
        self.agents = kept
        for aid in dropped:
            self._drop_peer(aid)
        if dropped:
            self._save()
        # re-arm the blocked re-announce for any agent restored still blocked, so
        # a daemon restart mid-herd doesn't silence a stuck worker until its next
        # hook (which a blocked agent won't fire until a human acts anyway)
        for aid, rec in self.agents.items():
            if rec.get("state") == "blocked":
                self._schedule_reemit(aid)

    def _save(self):
        if not self.state_path:
            return
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"agents": self.agents}, fh)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.state_path)
        except OSError:
            pass  # persistence is best-effort; the live bus is the truth

    def snapshot(self, agent_id):
        rec = self.agents.get(agent_id)
        # compacts/peers defaulted here so a record written by an older daemon
        # still answers the questions `get` is asked
        if not rec:
            return None
        return {"id": agent_id, "compacts": 0, "peers": [], "topics": {}, **rec}

    def report(self, agent_id, params):
        rec = self.agents.setdefault(agent_id, {
            "state": "unknown", "message": None, "agent": None,
            "pane": None, "cwd": None, "session": None, "transcript": None,
            "model": None, "effort": None, "compacts": 0,
            "peers": [], "topics": {}, "updated": 0.0,
        })
        state = params.get("state") or rec["state"]
        rec["state"] = state
        for key in MERGE_KEYS:
            value = params.get(key)
            if value == "":
                rec[key] = None  # explicit clear; absent/null means preserve
            elif value is not None:
                rec[key] = value
        if params.get("compact"):
            rec["compacts"] = int(rec.get("compacts") or 0) + 1
        rec["updated"] = round(time.time(), 3)
        self._save()
        event_type = "compacted" if params.get("compact") else "agent.state_changed"
        event = {"type": event_type, **self.snapshot(agent_id)}
        self._fanout(event, agent_id, state)
        self._wake(agent_id, state, event)
        if state == "blocked":
            self._schedule_reemit(agent_id)  # keep a stuck agent visible to late waiters
        else:
            self._cancel_reemit(agent_id)
        return event

    def remove(self, agent_id):
        rec = self.agents.pop(agent_id, None)
        if rec is None:
            return None
        self._cancel_reemit(agent_id)
        self._drop_peer(agent_id)
        self._save()
        event = {"type": "agent.removed", "id": agent_id}
        self._fanout(event, agent_id, None)
        return event

    def pair(self, a, b, topic, oneway=False):
        for aid in (a, b):
            if aid not in self.agents:
                return {"error": {"code": "not_found", "id": aid}}
        if a == b:
            return {"error": {"code": "invalid_params"}}
        self._link(a, b, topic)
        if not oneway:
            self._link(b, a, topic)
        self._save()
        return {"result": {"type": "paired", "id": a, "with": b, "topic": topic,
                           "oneway": bool(oneway),
                           "peers": self.agents[a]["peers"]}}

    def unpair(self, a, b):
        if a not in self.agents and b not in self.agents:
            return {"error": {"code": "not_found", "id": a}}
        for one, other in ((a, b), (b, a)):
            rec = self.agents.get(one)
            if rec is None:
                continue
            rec["peers"] = [p for p in (rec.get("peers") or []) if p != other]
            (rec.get("topics") or {}).pop(other, None)
        self._save()
        return {"result": {"type": "unpaired", "id": a, "with": b,
                           "peers": (self.agents.get(a) or {}).get("peers", [])}}

    def peer_send(self, params):
        # deliberately no record touched and no _wake: the pane carried the
        # message, so a master waiting on its own state must not be woken
        event = {"type": "peer_send", "from": params.get("from"),
                 "to": params.get("to"), "topic": params.get("topic"),
                 "chars": int(params.get("chars") or 0)}
        self._fanout(event, event["from"], None)
        return event

    def _link(self, one, other, topic):
        rec = self.agents[one]
        peers = rec.setdefault("peers", [])
        if other not in peers:
            peers.append(other)
        rec.setdefault("topics", {})[other] = topic

    def _drop_peer(self, agent_id):
        for rec in self.agents.values():
            if agent_id in (rec.get("peers") or []):
                rec["peers"] = [p for p in rec["peers"] if p != agent_id]
            (rec.get("topics") or {}).pop(agent_id, None)

    def _fanout(self, event, agent_id, state):
        for queue, id_f, state_f in list(self.subscribers):
            if id_f and id_f != agent_id:
                continue
            if state_f and state is not None and state_f != state:
                continue
            queue.put_nowait(event)

    def _wake(self, agent_id, state, event):
        remaining = []
        for predicate, fut in self.waiters:
            if fut.done():
                continue
            if predicate(agent_id, state, event):
                fut.set_result(event)
            else:
                remaining.append((predicate, fut))
        self.waiters = remaining

    def _schedule_reemit(self, agent_id):
        self._cancel_reemit(agent_id)
        if BLOCKED_REEMIT <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no daemon loop (e.g. a direct in-process report); nothing to arm
        self._reemit[agent_id] = loop.call_later(
            BLOCKED_REEMIT, self._reemit_blocked, agent_id)

    def _cancel_reemit(self, agent_id):
        handle = self._reemit.pop(agent_id, None)
        if handle is not None:
            handle.cancel()

    def _reemit_blocked(self, agent_id):
        rec = self.agents.get(agent_id)
        if not rec or rec.get("state") != "blocked":
            self._reemit.pop(agent_id, None)
            return
        # re-fire the wake so a waiter that registered AFTER the block still sees
        # it; deliberately not a _fanout, to keep `watch` a pure state-CHANGE
        # stream (monitor re-polls on its own tick anyway)
        event = {"type": "agent.state_changed", **self.snapshot(agent_id)}
        self._wake(agent_id, "blocked", event)
        self._schedule_reemit(agent_id)

    def _prunable(self, rec, now):
        age = now - rec.get("updated", 0)
        if rec.get("state") in TERMINAL and age > TERMINAL_TTL:
            return True
        return MAX_AGE > 0 and age > MAX_AGE

    def start_prune_sweeps(self):
        # periodic registry GC so a long-running daemon self-cleans without a
        # restart; kicked off once from _serve, then it reschedules itself
        if PRUNE_INTERVAL <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_later(PRUNE_INTERVAL, self._prune_sweep)

    def _prune_sweep(self):
        now = time.time()
        drop = [aid for aid, rec in self.agents.items() if self._prunable(rec, now)]
        for aid in drop:
            self._cancel_reemit(aid)
            self.agents.pop(aid, None)
            self._drop_peer(aid)
            self._fanout({"type": "agent.removed", "id": aid}, aid, None)
        if drop:
            self._save()
        self.start_prune_sweeps()

    def start_limit_sweeps(self, capture=None):
        if not LIMIT_SWEEP or LIMIT_INTERVAL <= 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._limit_timer = loop.call_later(
            LIMIT_INTERVAL, self._fire_limit_sweep, capture)

    def _fire_limit_sweep(self, capture):
        self._limit_task = asyncio.ensure_future(self.limit_sweep(capture))

    async def limit_sweep(self, capture=None):
        capture = capture or capture_pane
        loop = asyncio.get_running_loop()
        try:
            for aid in list(self.agents):
                try:
                    rec = self.agents.get(aid)
                    if not self._scrapable(rec):
                        continue
                    # off-loop: capture-pane on a dead pane or a missing tmux
                    # must never stall (or kill) the daemon
                    text = await loop.run_in_executor(None, capture, rec["pane"])
                    if not text or not limit_regex().search(limit_tail(text)):
                        continue
                    # the await gave the record time to move on, and a banner
                    # still on screen after an auto-resume must not re-flip it
                    rec = self.agents.get(aid)
                    if (not self._scrapable(rec)
                            or time.time() - rec.get("updated", 0) <= LIMIT_INTERVAL):
                        continue
                    _log("limited", aid, rec["pane"])
                    self.report(aid, {"state": "limited",
                                      "message": "usage limit banner in pane"})
                except Exception:
                    continue
        finally:
            self.start_limit_sweeps(capture)

    @staticmethod
    def _scrapable(rec):
        return bool(rec and rec.get("state") in LIMIT_WATCH and rec.get("pane"))


def _send(writer, obj):
    writer.write((json.dumps(obj) + "\n").encode())


async def _handle_client(reader, writer, bus):
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                _send(writer, {"error": {"code": "bad_json"}})
                continue

            rid = req.get("id")
            method = req.get("method")
            params = req.get("params") or {}

            if method == "ping":
                _send(writer, {"id": rid, "result": {"type": "pong", "version": __version__}})

            elif method == "agent.report":
                agent_id = params.get("id")
                if not agent_id:
                    _send(writer, {"id": rid, "error": {"code": "invalid_params"}})
                    continue
                event = bus.report(agent_id, params)
                _log("report", agent_id, event["state"], event.get("message") or "")
                _send(writer, {"id": rid, "result": {**event, "type": "reported"}})

            elif method == "agent.get":
                snap = bus.snapshot(params.get("id"))
                if snap is None:
                    _send(writer, {"id": rid, "error": {"code": "not_found"}})
                else:
                    _send(writer, {"id": rid, "result": {"type": "agent", **snap}})

            elif method == "agent.list":
                items = [bus.snapshot(a) for a in bus.agents]
                _send(writer, {"id": rid, "result": {"type": "agents", "agents": items}})

            elif method == "agent.remove":
                event = bus.remove(params.get("id"))
                if event is None:
                    _send(writer, {"id": rid, "error": {"code": "not_found"}})
                else:
                    _log("remove", params.get("id"))
                    _send(writer, {"id": rid, "result": {**event, "type": "removed"}})

            elif method in ("agent.pair", "agent.unpair"):
                one, other = params.get("id"), params.get("with")
                if not one or not other:
                    _send(writer, {"id": rid, "error": {"code": "invalid_params"}})
                    continue
                if method == "agent.pair":
                    if not params.get("topic"):
                        _send(writer, {"id": rid, "error": {"code": "invalid_params"}})
                        continue
                    resp = bus.pair(one, other, params.get("topic"),
                                    params.get("oneway"))
                else:
                    resp = bus.unpair(one, other)
                if "result" in resp:
                    _log(method.split(".")[1], one, other)
                _send(writer, {"id": rid, **resp})

            elif method == "peer.send":
                event = bus.peer_send(params)
                _log("peer_send", f"{event['from']} -> {event['to']}",
                     f"{event['chars']} chars", event["topic"] or "")
                _send(writer, {"id": rid, "result": event})

            elif method == "wait":
                await _handle_wait(writer, bus, rid, params)

            elif method == "subscribe":
                await _handle_subscribe(reader, writer, bus, rid, params)
                return  # subscribe owns the connection until it drops

            else:
                _send(writer, {"id": rid, "error": {"code": "unknown_method"}})
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _handle_wait(writer, bus, rid, params):
    ids = params.get("ids") or ([params["id"]] if params.get("id") else [])
    prefix = params.get("prefix")
    states = params.get("states") or ([params["state"]] if params.get("state") else None)
    on_compact = bool(params.get("on_compact"))
    if (not ids and not prefix) or (not states and not on_compact):
        _send(writer, {"id": rid, "error": {"code": "invalid_params"}})
        return

    def matches(agent_id):
        return agent_id in ids or (prefix is not None and agent_id.startswith(prefix))

    def matched_now():
        # every currently-matching agent already in a target state, so a herd
        # wait can batch-collect them instead of re-issuing the wait per straggler
        return [bus.snapshot(a) for a in bus.agents
                if states and matches(a) and bus.agents[a]["state"] in states]

    if not params.get("edge"):
        ready = matched_now()
        if ready:
            _send(writer, {"id": rid, "result": {
                **ready[0], "type": "waited", "already": True, "matched": ready}})
            return

    fut = asyncio.get_running_loop().create_future()
    entry = (lambda i, s, e: matches(i) and (
        (states and s in states) or (on_compact and e.get("type") == "compacted")), fut)
    bus.waiters.append(entry)
    timeout = params.get("timeout_ms")
    try:
        event = await asyncio.wait_for(fut, timeout / 1000.0 if timeout else None)
        result_type = "compacted" if event.get("type") == "compacted" else "waited"
        result = {**event, "type": result_type, "already": False}
        if result_type == "waited":
            result["matched"] = matched_now()
        _send(writer, {"id": rid, "result": result})
    except asyncio.TimeoutError:
        _send(writer, {"id": rid, "error": {"code": "timeout", "agents": ids,
                                            "prefix": prefix, "states": states}})
    finally:
        if entry in bus.waiters:
            bus.waiters.remove(entry)


async def _handle_subscribe(reader, writer, bus, rid, params):
    queue = asyncio.Queue()
    entry = (queue, params.get("id"), params.get("state"))
    bus.subscribers.add(entry)
    _send(writer, {"id": rid, "result": {"type": "subscribed"}})
    await writer.drain()

    async def pump():
        while True:
            _send(writer, await queue.get())
            await writer.drain()

    async def watch_eof():
        while await reader.readline():
            pass

    tasks = [asyncio.ensure_future(pump()), asyncio.ensure_future(watch_eof())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        bus.subscribers.discard(entry)


def limit_regex():
    global LIMIT_RE
    if LIMIT_RE is None:
        pattern = os.environ.get("HERDLET_LIMIT_PATTERN", LIMIT_PATTERN_DEFAULT)
        try:
            LIMIT_RE = re.compile(pattern, re.I)
        except re.error as exc:
            _log(f"bad HERDLET_LIMIT_PATTERN ({exc}); using the default")
            LIMIT_RE = re.compile(LIMIT_PATTERN_DEFAULT, re.I)
    return LIMIT_RE


async def _serve(sock_path):
    limit_regex()  # compile here, so a bad pattern is logged once, by the daemon
    bus = Bus(state_path=sock_path + ".state")
    bus.start_prune_sweeps()
    bus.start_limit_sweeps()
    server = await asyncio.start_unix_server(
        lambda r, w: _handle_client(r, w, bus), path=sock_path)
    os.chmod(sock_path, 0o600)
    _log(f"herdlet {__version__} listening on {sock_path}")

    stop = asyncio.get_running_loop().create_future()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(
            sig, lambda: stop.done() or stop.set_result(None))
    async with server:
        await stop


def daemon_running(sock_path):
    try:
        probe = socket.socket(socket.AF_UNIX)
        probe.settimeout(0.5)
        probe.connect(sock_path)
        probe.close()
        return True
    except OSError:
        return False


def cmd_serve(args):
    if os.path.exists(args.socket):
        if daemon_running(args.socket):
            if args.if_needed:
                return 0
            print(f"herdlet already running on {args.socket}", file=sys.stderr)
            return 1
        os.unlink(args.socket)  # stale socket from a dead daemon
    try:
        asyncio.run(_serve(args.socket))
    finally:
        try:
            os.unlink(args.socket)
        except OSError:
            pass
    return 0


def call(sock_path, method, params, timeout=5.0):
    conn = socket.socket(socket.AF_UNIX)
    conn.settimeout(timeout)
    conn.connect(sock_path)
    stream = conn.makefile("rwb")
    stream.write((json.dumps({"id": "1", "method": method, "params": params}) + "\n").encode())
    stream.flush()
    line = stream.readline()
    conn.close()
    if not line:
        raise ConnectionResetError("daemon closed the connection")
    return json.loads(line)


def call_or_die(sock_path, method, params, timeout=5.0):
    try:
        return call(sock_path, method, params, timeout)
    except (FileNotFoundError, ConnectionRefusedError):
        die(f"herdlet daemon is not running on {sock_path} (start it with: herdlet serve)")


def warn_version_skew(sock_path):
    # a daemon from a previous install serves the old protocol: no limit sweep,
    # no transcript/model/effort merge keys, no compacts counter
    try:
        version = call(sock_path, "ping", {}, timeout=1.0)["result"]["version"]
    except (OSError, ValueError, KeyError, TypeError):
        return
    if version != __version__:
        print(f"herdlet: daemon is {version}, client is {__version__}; "
              f"restart it: pkill -f 'herdlet.*serve' then any herdlet command",
              file=sys.stderr)


def ensure_daemon(sock_path):
    if daemon_running(sock_path):
        return True
    with open(LOG_PATH, "ab") as log:
        subprocess.Popen(
            [sys.executable, os.path.realpath(__file__), "--socket", sock_path,
             "serve", "--if-needed"],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
    for _ in range(20):
        if daemon_running(sock_path):
            return True
        time.sleep(0.1)
    return False


def die(msg):
    print(f"herdlet: {msg}", file=sys.stderr)
    sys.exit(1)


def emit(resp):
    print(json.dumps(resp, indent=2))
    if resp and "error" in resp:
        sys.exit(2 if resp["error"].get("code") == "timeout" else 1)


def default_id():
    return os.environ.get("HERDLET_ID") or os.environ.get("TMUX_PANE")


def squash(text, limit=120):
    return " ".join(str(text).split())[:limit]


def tmux_run(*args, input=None, timeout=5):
    try:
        return subprocess.run(("tmux",) + args, capture_output=True, text=True,
                              timeout=timeout, input=input)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None


def tmux(*args, check=False, input=None):
    out = tmux_run(*args, input=input)
    if out is None:
        if check:
            die("tmux not available")
        return None
    if out.returncode != 0:
        if check:
            die(f"tmux {' '.join(args)}: {out.stderr.strip()}")
        return None
    return out.stdout


def capture_pane(pane):
    """Visible pane text, or '' if tmux or the pane is gone."""
    return tmux("capture-pane", "-p", "-J", "-t", pane) or ""


def limit_tail(text, lines=LIMIT_TAIL):
    return "\n".join([l for l in text.splitlines() if l.strip()][-lines:])


def pane_map():
    out = tmux("list-panes", "-a", "-F",
               "#{pane_id}\t#{session_name}\t#{window_id}\t#{window_index}"
               "\t#{window_name}\t#{pane_current_command}")
    panes = {}
    for line in (out or "").splitlines():
        pane, session, window_id, window_index, window_name, command = \
            (line.split("\t") + [""] * 6)[:6]
        panes[pane] = {"session": session, "window_id": window_id,
                       "window_index": window_index, "window_name": window_name,
                       "command": command}
    return panes


def resolve_pane(sock_path, agent_id):
    """Registered agent id -> its pane; otherwise treat the id as a tmux target."""
    try:
        resp = call(sock_path, "agent.get", {"id": agent_id})
        pane = resp.get("result", {}).get("pane")
        if pane:
            return pane
    except OSError:
        pass
    if agent_id.startswith("%"):
        return agent_id
    die(f"unknown agent '{agent_id}' (see: herdlet list)")


def cmd_ping(args):
    emit(call_or_die(args.socket, "ping", {}))


def cmd_report(args):
    agent_id = args.id or default_id()
    if not agent_id:
        die("no agent id: pass --id, or set HERDLET_ID, or run inside tmux")
    params = {"id": agent_id, "state": args.state, "pane": args.pane or os.environ.get("TMUX_PANE")}
    if args.message is not None:
        params["message"] = args.message
    if args.agent:
        params["agent"] = args.agent
    if args.cwd:
        params["cwd"] = args.cwd
    if args.session:
        params["session"] = args.session
    ensure_daemon(args.socket)
    emit(call_or_die(args.socket, "agent.report", params))


def cmd_get(args):
    emit(call_or_die(args.socket, "agent.get", {"id": args.id or default_id()}))


def pane_kill_allowed(rec, command):
    return rec.get("state") in ("done", "ended") or command in SHELLS


def kill_record_pane(agent_id, rec):
    pane = rec.get("pane")
    if not pane:
        print(f"{agent_id}: no pane to kill", file=sys.stderr)
        return False
    status = (tmux("display-message", "-p", "-t", pane,
                   "#{pane_id}\t#{pane_current_command}") or "").strip()
    if not status:
        print(f"{agent_id}: pane {pane} is gone", file=sys.stderr)
        return False
    current = (status.split("\t", 1) + [""])[:2][1]
    if not pane_kill_allowed(rec, current):
        print(f"{agent_id}: pane {pane} still runs {current}; not killed",
              file=sys.stderr)
        return False
    tmux("kill-pane", "-t", pane, check=True)
    print(f"{agent_id}: killed pane {pane}")
    return True


def cmd_remove(args):
    agent_id = args.id or default_id()
    if not agent_id:
        die("no agent id: pass --id, or set HERDLET_ID, or run inside tmux")
    rec = call_or_die(args.socket, "agent.get", {"id": agent_id}).get("result")
    if args.kill_pane and rec:
        kill_record_pane(agent_id, rec)
    emit(call_or_die(args.socket, "agent.remove", {"id": agent_id}))


def cmd_pair(args):
    resp = call_or_die(args.socket, "agent.pair", {
        "id": args.id, "with": args.peer, "topic": os.path.abspath(args.topic)})
    _die_on_pair_error(resp)
    emit(resp)


def cmd_unpair(args):
    resp = call_or_die(args.socket, "agent.unpair",
                       {"id": args.id, "with": args.peer})
    _die_on_pair_error(resp)
    emit(resp)


def _die_on_pair_error(resp):
    err = resp.get("error") or {}
    if err.get("code") == "not_found":
        die(f"unknown agent '{err.get('id')}' (see: herdlet list)")
    if err.get("code") == "invalid_params":
        die("pair takes two different registered ids and a --topic path")
    if err.get("code") == "unknown_method":
        die(stale_daemon_note())


def stale_daemon_note():
    return (f"the running daemon has no peer channel (herdlet {__version__} "
            f"needs its own daemon); restart it: pkill -f 'herdlet.*serve'")


def sort_key(rec):
    order = {"blocked": 0, "limited": 1, "stale": 2, "gone": 3, "working": 4,
             "spawning": 5, "done": 6, "ended": 7, "idle": 8}
    return (order.get(rec["state"], 9), -rec["updated"])


def age(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def annotate(agents, panes):
    now = time.time()
    for rec in agents:
        pane = rec.get("pane")
        if pane and panes and pane not in panes:
            rec["state"] = "gone"
        info = panes.get(pane) if pane else None
        # agent process exited without a hook firing (deny, crash, ctrl-c): the
        # pane is back at a bare shell AND the record has gone quiet. The
        # freshness check is what keeps a just-spawned or actively-hooking worker
        # (whose pane_current_command is a shell) from being called stale while
        # it is plainly alive - the friction that made one-shot workers unusable.
        if (info and rec["state"] in ("working", "blocked", "spawning", "limited")
                and info.get("command") in SHELLS
                and now - rec["updated"] > STALE_AFTER):
            rec["state"] = "stale"
        rec["where"] = f"{info['session']}:{info['window_index']} {info['window_name']}" if info else ""
        rec["age"] = age(now - rec["updated"])
    agents.sort(key=sort_key)
    return agents


def filter_agents(agents, session=None, here=False, prefix=None):
    if here:
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            die("--here requires running inside tmux")
        session = (tmux("display-message", "-p", "-t", pane,
                        "#{session_name}", check=True) or "").strip()
    if session:
        agents = [a for a in agents if a["where"].split(":")[0] == session]
    if prefix:
        agents = [a for a in agents if a["id"].startswith(prefix)]
    return agents


def model_cell(rec):
    parts = [rec.get("model"), rec.get("effort")]
    return "/".join(p for p in parts if p)


def state_cell(rec):
    state = rec["state"]
    compacts = int(rec.get("compacts") or 0)
    return f"{state} C{compacts}" if compacts else state


def cmd_list(args):
    resp = call_or_die(args.socket, "agent.list", {})
    agents = annotate(resp.get("result", {}).get("agents", []), pane_map())
    agents = filter_agents(agents, args.session, args.here, args.prefix)
    if args.json:
        print(json.dumps(agents, indent=2))
        return
    if not agents:
        print("no agents registered")
        return
    # PEERS only when someone has one, to keep the usual table narrow
    peers = any(rec.get("peers") for rec in agents)
    header = ["ID", "STATE", "AGE", "AGENT", "MODEL", "PANE", "WHERE"]
    rows = [tuple(header + (["PEERS"] if peers else []) + ["MESSAGE"])]
    for rec in agents:
        cells = [rec["id"], state_cell(rec), rec["age"], rec.get("agent") or "-",
                 model_cell(rec), rec.get("pane") or "-", rec["where"]]
        if peers:
            cells.append(",".join(rec.get("peers") or []) or "-")
        rows.append(tuple(cells + [rec.get("message") or ""]))
    last = len(rows[0]) - 1
    widths = [max(len(row[i]) for row in rows) for i in range(last)]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:last]))
              + "  " + row[last])


def timeout_as_result(resp):
    """Rewrite a timeout error as a result, client-side so old daemons work too."""
    err = (resp or {}).get("error") or {}
    if err.get("code") != "timeout":
        return resp
    result = {"type": "timeout",
              "agents": err.get("agents") or ([err["id"]] if err.get("id") else []),
              "states": err.get("states") or []}
    for key in ("prefix", "match"):
        if err.get(key):
            result[key] = err[key]
    out = {"result": result}
    if resp.get("id") is not None:
        out = {"id": resp["id"], **out}
    return out


def wait_for_match(args, ids):
    if args.prefix or len(ids) != 1:
        die("--match takes exactly one --id and no --prefix")
    if args.state:
        die("--match and --state are mutually exclusive")
    try:
        rx = re.compile(args.match)
    except re.error as exc:
        die(f"invalid regex: {exc}")
    pane = resolve_pane(args.socket, ids[0])
    deadline = time.time() + args.timeout if args.timeout else None
    while True:
        out = tmux("capture-pane", "-p", "-J", "-t", pane, "-S", f"-{args.lines}", check=True) or ""
        for line in out.splitlines():
            if rx.search(line):
                print(json.dumps({"result": {"type": "output_matched",
                                             "id": ids[0], "line": line}}, indent=2))
                return 0
        if deadline is not None and time.time() >= deadline:
            resp = {"error": {"code": "timeout", "id": ids[0], "match": args.match}}
            if args.timeout_ok:
                print(json.dumps(timeout_as_result(resp), indent=2))
                return 0
            print(json.dumps(resp, indent=2))
            return 2
        time.sleep(2)


def cmd_wait(args):
    ids = [s.strip() for s in (args.id or "").split(",") if s.strip()]
    if not ids and not args.prefix:
        die("pass --id (comma-separated waits on whichever transitions first) and/or --prefix")
    if args.edge and args.match:
        die("--edge is meaningless with --match: a match poll never checks stored state")
    if args.on_compact and args.match:
        die("--on-compact and --match are mutually exclusive")
    if args.match:
        return wait_for_match(args, ids)
    if not args.state and not args.on_compact:
        die("pass --state, --on-compact, or --match")
    states = [s.strip() for s in (args.state or "").split(",") if s.strip()]
    params = {"states": states}
    if len(ids) == 1 and not args.prefix:
        params["id"] = ids[0]  # single-id shape, keeps pre-0.3 daemons working
    else:
        if ids:
            params["ids"] = ids
        if args.prefix:
            params["prefix"] = args.prefix
    if args.edge:
        params["edge"] = True  # only send when set, so older daemons still work
    if args.on_compact:
        params["on_compact"] = True
    if args.timeout:
        params["timeout_ms"] = int(args.timeout * 1000)
    client_timeout = args.timeout + 5 if args.timeout else None
    try:
        resp = call(args.socket, "wait", params, timeout=client_timeout)
        emit(timeout_as_result(resp) if args.timeout_ok else resp)
    except (FileNotFoundError, ConnectionRefusedError):
        die(f"herdlet daemon is not running on {args.socket} (start it with: herdlet serve)")


def cmd_watch(args):
    params = {}
    if args.id:
        params["id"] = args.id
    if args.state:
        params["state"] = args.state
    try:
        conn = socket.socket(socket.AF_UNIX)
        conn.connect(args.socket)
    except OSError:
        die(f"herdlet daemon is not running on {args.socket}")
    stream = conn.makefile("rwb")
    stream.write((json.dumps({"id": "w", "method": "subscribe", "params": params}) + "\n").encode())
    stream.flush()
    stream.readline()  # subscribed ack
    try:
        for line in stream:
            print(line.decode().rstrip(), flush=True)
    except KeyboardInterrupt:
        pass


HOOK_STATES = {
    "SessionStart": "idle",
    "UserPromptSubmit": "working",
    "PreToolUse": "working",
    "PostToolUse": "working",
    "Notification": "blocked",
    "PermissionRequest": "blocked",
    "Stop": "done",
}

HOOK_CMD = "command -v herdlet >/dev/null 2>&1 && herdlet hook || true"
CODEX_HOOK_CMD = ("command -v herdlet >/dev/null 2>&1 && "
                  "herdlet hook --agent codex --event {event} || true")
NOTIFY_MATCHER = "permission_prompt|elicitation_dialog"
CLAUDE_EVENTS = ("SessionStart", "SessionEnd", "UserPromptSubmit",
                 "PostToolUse", "Notification", "PreCompact", "Stop")
CODEX_EVENTS = ("UserPromptSubmit", "PreToolUse", "PermissionRequest", "Stop")


def _load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        die(f"{path} is not valid JSON; fix or move it aside first")


def _save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + ".herdlet-bak")
    # open for write (not replace) so stow/dotfiles symlinks stay symlinks
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


def _wire_hooks(cfg, events, command_for, matcher_for):
    hooks = cfg.setdefault("hooks", {})
    added = []
    for event in events:
        groups = hooks.setdefault(event, [])
        if any("herdlet hook" in h.get("command", "")
               for g in groups for h in g.get("hooks", [])):
            continue
        group = {"hooks": [{"type": "command", "command": command_for(event)}]}
        matcher = matcher_for(event)
        if matcher:
            group["matcher"] = matcher
        groups.append(group)
        added.append(event)
    return added


def _skill_source():
    here = os.path.dirname(os.path.realpath(__file__))
    for cand in (os.path.join(here, "skills", "herdlet", "SKILL.md"),
                 os.path.normpath(os.path.join(here, "..", "share", "doc",
                                               "herdlet", "SKILL.md"))):
        if os.path.exists(cand):
            return cand
    return None


def _install_skill(dest_dir):
    dest = os.path.join(dest_dir, "herdlet", "SKILL.md")
    if os.path.lexists(dest):
        return f"{dest}: already present, skipped"
    source = _skill_source()
    if source is None:
        return "SKILL.md not found next to this install, skipped"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(source, dest)
    return f"{dest}: installed"


def _opencode_plugin_source():
    here = os.path.dirname(os.path.realpath(__file__))
    for cand in (os.path.join(here, "integrations", "opencode", "herdlet.js"),
                 os.path.normpath(os.path.join(here, "..", "share", "doc",
                                               "herdlet", "opencode-herdlet.js"))):
        if os.path.exists(cand):
            return cand
    return None


def _install_opencode_plugin():
    # opencode auto-loads any *.js/*.ts in its global plugins dir. Unlike Claude
    # and Codex it has no shell-hook config, so the bridge is a plugin file.
    dest = os.path.expanduser("~/.config/opencode/plugins/herdlet.js")
    if os.path.lexists(dest):
        return f"{dest}: already present, skipped"
    source = _opencode_plugin_source()
    if source is None:
        return "opencode plugin not found next to this install, skipped"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(source, dest)
    return f"{dest}: installed"


def cmd_setup(args):
    home = os.path.expanduser("~")

    path = os.path.join(home, ".claude", "settings.json")
    cfg = _load_json(path)
    added = _wire_hooks(cfg, CLAUDE_EVENTS, lambda e: HOOK_CMD,
                        lambda e: NOTIFY_MATCHER if e == "Notification" else None)
    allow = cfg.setdefault("permissions", {}).setdefault("allow", [])
    rules = ["Bash(herdlet:*)"] + (["Bash(tmux:*)"] if args.allow_tmux else [])
    new_rules = [r for r in rules if r not in allow]
    allow.extend(new_rules)
    if added or new_rules:
        _save_json(path, cfg)
    print(f"claude hooks : {'wired ' + ', '.join(added) if added else 'already wired'}")
    print(f"claude perms : {'allowed ' + ', '.join(new_rules) if new_rules else 'already allowed'}")

    path = os.path.join(home, ".codex", "hooks.json")
    cfg = _load_json(path)
    added = _wire_hooks(cfg, CODEX_EVENTS,
                        lambda e: CODEX_HOOK_CMD.format(event=e), lambda e: None)
    if added:
        _save_json(path, cfg)
    print(f"codex hooks  : {'wired ' + ', '.join(added) if added else 'already wired'}")

    print(f"opencode plug: {_install_opencode_plugin()}")

    print(f"claude skill : {_install_skill(os.path.join(home, '.claude', 'skills'))}")
    print(f"codex skill  : {_install_skill(os.path.join(home, '.codex', 'skills'))}")
    print("\nrunning agent sessions pick this up on restart. optional tmux popup:")
    print('  bind m display-popup -E -w 80% -h 60% -T " agents " "herdlet monitor"')
    return 0


def cmd_hook(args):
    # fired from agent hook chains: never block, never fail, never print
    if os.environ.get("HERDLET_SKIP"):
        # nested/utility agent runs (e.g. a Stop hook summarizing via `claude -p`)
        # inherit the real agent's TMUX_PANE/HERDLET_ID and would corrupt its record
        return 0
    try:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except Exception:
        data = {}
    try:
        event = args.event or data.get("hook_event_name") or ""
        agent_id = args.id or default_id() or (data.get("session_id") or "")[:8]
        if not agent_id:
            return 0

        if event == "SessionEnd":
            # Keep the record instead of deleting it: a finished OR crashed agent
            # stays visible in `list` and, crucially, keeps its session ref so
            # `herdlet resume` can bring it back. `remove` (or `ack`) is the
            # explicit way to clear it. pane/cwd are preserved by omission
            # (absent != "" clear); only session is refreshed if the event has one.
            params = {"id": agent_id, "state": "ended", "agent": args.agent}
            if data.get("session_id"):
                params["session"] = squash(str(data["session_id"]), 200)
            if not ensure_daemon(args.socket):
                return 0
            call(args.socket, "agent.report", params, timeout=1.0)
            return 0

        if event == "PreCompact":
            if not ensure_daemon(args.socket):
                return 0
            # a compaction alone says nothing about state, so an id with no
            # record yet must not be registered as `unknown`
            if "result" not in call(args.socket, "agent.get",
                                    {"id": agent_id}, timeout=1.0):
                return 0
            call(args.socket, "agent.report",
                 {"id": agent_id, "agent": args.agent, "compact": True,
                  "pane": os.environ.get("TMUX_PANE")}, timeout=1.0)
            return 0

        state = HOOK_STATES.get(event)
        if state is None:
            return 0

        params = {"id": agent_id, "state": state,
                  "agent": args.agent,
                  "pane": os.environ.get("TMUX_PANE"),
                  "cwd": data.get("cwd") or os.getcwd()}
        # the agent's native session ref enables `herdlet resume` later
        if data.get("session_id"):
            params["session"] = squash(str(data["session_id"]), 200)
        if data.get("transcript_path"):
            params["transcript"] = squash(str(data["transcript_path"]), 500)
        # message: prompt/notification text is worth showing; tool events pass
        # None so the daemon preserves the prompt across the whole turn
        if event == "UserPromptSubmit":
            params["message"] = squash(data.get("prompt") or "")
        elif event in ("Notification", "PermissionRequest"):
            params["message"] = squash(data.get("message") or "awaiting approval")
        elif event in ("SessionStart", "Stop"):
            params["message"] = ""  # a starting/finished turn shows no stale "doing" text

        if not ensure_daemon(args.socket):
            return 0
        call(args.socket, "agent.report", params, timeout=1.0)
    except Exception:
        pass
    return 0


# Typed character by character, a long message races the Enter that follows it
# and gets submitted in half. A bracketed paste is one atomic block, so route
# anything long that way; it also sidesteps tmux's 16 KiB command-line ceiling.
SEND_PASTE_OVER = 200
SEND_POLL_INTERVAL = 0.1


def pane_input_text(capture):
    """Pending TUI input, '' for an empty box, or None when no box is visible."""
    lines = capture.replace("\u00a0", " ").splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    for index in range(len(lines) - 1, max(-1, len(lines) - 13), -1):
        match = re.match(r"^\s*([>❯›])\s*(.*?)\s*$", lines[index])
        if not match:
            continue
        marker, first = match.groups()
        if re.match(r"\d+\.\s", first):
            return None
        if marker == "›" and first.lower() == "ask codex to do anything":
            return ""
        parts = [first] if first else []
        for line in lines[index + 1:]:
            stripped = line.strip()
            if not stripped or re.fullmatch(r"─+", stripped):
                break
            parts.append(stripped)
        return "\n".join(parts)
    return None


def wait_for_empty_input(pane, settle):
    deadline = time.monotonic() + max(0, settle)
    while True:
        pending = pane_input_text(capture_pane(pane))
        if not pending:
            return pending
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return pending
        time.sleep(min(SEND_POLL_INTERVAL, remaining))


def send_lock(sock_path, pane):
    state_dir = sock_path + ".state.d"
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", pane)
    lock = open(os.path.join(state_dir, f"send-{name}.lock"), "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def send_text(pane, text, no_enter=False):
    if "\n" in text or len(text) > SEND_PASTE_OVER:
        # bracketed paste: a readline TUI takes the newlines as text, not submits
        buf = f"herdlet-send-{os.getpid()}"
        tmux("load-buffer", "-b", buf, "-", input=text, check=True)
        tmux("paste-buffer", "-p", "-d", "-b", buf, "-t", pane, check=True)
    else:
        tmux("send-keys", "-t", pane, "-l", "--", text, check=True)
    if not no_enter:
        time.sleep(0.2)  # let the TUI ingest the text before submit
        tmux("send-keys", "-t", pane, "Enter", check=True)


def verified_send(pane, text, settle):
    pending = wait_for_empty_input(pane, settle)
    quiesced = not bool(pending)
    send_text(pane, text)
    pending = wait_for_empty_input(pane, settle)
    if pending:
        tmux("send-keys", "-t", pane, "Enter", check=True)
        pending = wait_for_empty_input(pane, settle)
    return quiesced, not bool(pending)


THREAD_HEADING = "## Thread"


def thread_line(from_id, to_id, text):
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return f"- {stamp} {from_id} -> {to_id}: {squash(text)}"


def append_thread(path, line):
    """One line under `## Thread`, heading created once. Returns an error note."""
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a+") as fh:
            fh.seek(0)
            body = fh.read()
            out = []
            if body and not body.endswith("\n"):
                out.append("\n")
            if not any(l.strip() == THREAD_HEADING for l in body.splitlines()):
                out.append(f"\n{THREAD_HEADING}\n\n" if body else f"{THREAD_HEADING}\n\n")
            out.append(line + "\n")
            fh.write("".join(out))
    except OSError as exc:
        return f"sent, but could not log it to {path}: {exc}"
    return None


def peer_scope(sock_path, target):
    """(caller id, topic) for a worker's peer send; (None, None) for the master.

    A worker may only reach a peer the master paired it with; everything else
    belongs in its report, not in a side channel.
    """
    caller = os.environ.get("HERDLET_ID")
    if not caller:
        return None, None
    resp = call_or_die(sock_path, "agent.get", {"id": caller})
    err = resp.get("error") or {}
    if err.get("code") == "not_found":
        # no record means no peers, but the cause is a missing registration or a
        # daemon restart, not a scope decision; say which
        die(f"no record for {caller}; is the daemon current?")
    rec = resp.get("result") or {}
    if "peers" not in rec:
        die(stale_daemon_note())  # a current daemon always defaults the key
    if target not in (rec.get("peers") or []):
        print(f"herdlet: not paired with {target}; "
              f"raise it in your report to the master", file=sys.stderr)
        sys.exit(3)
    return caller, (rec.get("topics") or {}).get(target)


def record_peer_send(sock_path, from_id, to_id, topic, text):
    if topic:
        note = append_thread(topic, thread_line(from_id, to_id, text))
        if note:
            print(f"herdlet: {note}", file=sys.stderr)
    try:
        resp = call(sock_path, "peer.send", {"from": from_id, "to": to_id,
                                             "topic": topic, "chars": len(text)},
                    timeout=1.0)
    except (OSError, ValueError):
        return  # the message is already in the pane; the event is a nicety
    if (resp.get("error") or {}).get("code") == "unknown_method":
        print(f"herdlet: sent, but no peer_send event: {stale_daemon_note()}",
              file=sys.stderr)


def send_record(sock_path, agent_id):
    try:
        return call(sock_path, "agent.get", {"id": agent_id}).get("result")
    except OSError:
        return None


def wait_for_send_ack(sock_path, agent_id, text, updated, settle):
    deadline = time.monotonic() + max(0, settle)
    expected = squash(text)
    while True:
        record = send_record(sock_path, agent_id)
        if (record and record.get("state") == "working"
                and record.get("message") == expected
                and record.get("updated", 0) > updated):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(SEND_POLL_INTERVAL, remaining))


def cmd_send(args):
    if args.text and args.file:
        die("pass either positional text or --file PATH ('-' for stdin), not both")
    if not args.text and not args.file:
        die("nothing to send: pass text or --file PATH")
    if args.file:
        try:
            text = sys.stdin.read() if args.file == "-" else open(args.file).read()
        except OSError as exc:
            die(f"cannot read {args.file}: {exc}")
        text = text.rstrip("\n")
    else:
        text = " ".join(args.text)
    if not text:
        die(f"nothing to send: {args.file} is empty" if args.file
            else "nothing to send: the message is empty")
    if args.no_enter and not args.no_verify:
        die("--no-enter requires --no-verify")
    if args.ack and args.no_verify:
        die("--ack requires verification")
    if args.settle < 0:
        die("--settle must be zero or more")
    from_id, topic = peer_scope(args.socket, args.id)
    record = send_record(args.socket, args.id)
    pane = record.get("pane") if record else resolve_pane(args.socket, args.id)
    if not pane:
        die(f"'{args.id}' has no pane")
    lock = send_lock(args.socket, pane)
    acknowledged = None
    try:
        if args.no_verify:
            send_text(pane, text, args.no_enter)
            verified = False
        else:
            quiesced, verified = verified_send(pane, text, args.settle)
            if not quiesced:
                print(f"herdlet: input in {args.id} did not clear within "
                      f"{args.settle:g}s; sending anyway", file=sys.stderr)
            if not verified:
                print(f"herdlet: send to {args.id} not submitted; "
                      "text is sitting in its prompt", file=sys.stderr)
                return 4
        if args.ack:
            if record is None:
                print(f"herdlet: send to {args.id} has no hook record; "
                      "skipped ack", file=sys.stderr)
                acknowledged = False
            else:
                acknowledged = wait_for_send_ack(
                    args.socket, args.id, text, record.get("updated", 0), args.settle)
                if not acknowledged:
                    print(f"herdlet: send to {args.id} was submitted, but no "
                          "hook acknowledgment arrived", file=sys.stderr)
                    return 4
        if from_id:
            record_peer_send(args.socket, from_id, args.id, topic, text)
    finally:
        lock.close()
    if args.json:
        print(json.dumps({"result": {
            "type": "sent", "id": args.id, "pane": pane,
            "verified": verified, "acknowledged": acknowledged,
        }}, indent=2))
    return 0


def codex_transcript_message(row):
    payload = row.get("payload")
    if not isinstance(payload, dict):
        return None
    if (row.get("type") == "response_item" and payload.get("type") == "message"
            and payload.get("role") == "assistant"):
        text = "\n".join(
            block.get("text") or "" for block in payload.get("content") or []
            if isinstance(block, dict) and block.get("type") == "output_text"
        ).strip()
        return ("response_item", text) if text else None
    if row.get("type") == "event_msg" and payload.get("type") == "task_complete":
        text = payload.get("last_agent_message")
        if isinstance(text, str) and text.strip():
            return "task_complete", text.strip()
    return None


def transcript_messages(path, count):
    """Last `count` (timestamp, text) assistant messages of a jsonl, oldest first."""
    found = []
    codex_sources = {}
    with open(path, errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            codex = codex_transcript_message(row)
            if codex:
                source, text = codex
                previous = codex_sources.get(text)
                if previous and previous != source:
                    continue
                codex_sources[text] = source
                found.append((row.get("timestamp"), text))
                continue
            if row.get("type") != "assistant":
                continue
            blocks = (row.get("message") or {}).get("content")
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks}]
            text = "\n".join(b.get("text") or "" for b in blocks or []
                             if isinstance(b, dict) and b.get("type") == "text").strip()
            if text:
                found.append((row.get("timestamp"), text))
    return found[-count:] if count > 0 else found


def peek_transcript(args, lines):
    rec = call_or_die(args.socket, "agent.get", {"id": args.id}).get("result")
    if rec is None:
        die(f"unknown agent '{args.id}' (see: herdlet list)")
    path = rec.get("transcript")
    if not path:
        die(f"no transcript recorded for {args.id}; use plain peek")
    if not os.path.exists(path):
        die(f"recorded transcript is gone: {path}")
    messages = transcript_messages(path, lines)
    if not messages:
        die(f"no assistant messages in {path}")
    for stamp, text in messages:
        print(f"--- assistant {stamp} ---" if stamp else "--- assistant ---")
        print(text)


def cmd_peek(args):
    lines = args.lines if args.lines is not None else (1 if args.transcript else 60)
    lines = max(1, lines)
    if args.transcript:
        return peek_transcript(args, lines)
    pane = resolve_pane(args.socket, args.id)
    flags = ["-p", "-J"] if args.join else ["-p"]
    out = tmux("capture-pane", *flags, "-t", pane, "-S", f"-{lines}", check=True)
    print(out.rstrip("\n"))


SPAWN_ENV = ("CC_IMESSAGE_SKIP=1", "CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1")


def brief_title(path):
    """First markdown heading of a brief, which is what the worker is for."""
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                match = re.match(r"\s*#+\s+(.+?)\s*$", line)
                if match:
                    return squash(match.group(1))
    except OSError:
        pass
    return None


def spawn_line(agent_id, model, effort, title, permission_mode,
               env=(), program="claude", program_args=(), sandbox=None,
               approval=None):
    parts = list(SPAWN_ENV) + [f"HERDLET_ID={shlex.quote(agent_id)}"]
    for pair in env:
        key, sep, value = pair.partition("=")
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            die(f"invalid --env pair: {pair}")
        parts.append(f"{key}={shlex.quote(value)}")
    if program == "claude":
        parts += ["claude",
                  "-n", shlex.quote(f"{agent_id}: {title}"),
                  "--model", shlex.quote(model),
                  "--effort", shlex.quote(effort),
                  "--permission-mode", shlex.quote(permission_mode)]
    elif program == "codex":
        parts += ["codex",
                  "-m", shlex.quote(model),
                  "-c", shlex.quote(f"model_reasoning_effort={effort}"),
                  "--sandbox", shlex.quote(sandbox or "workspace-write"),
                  "-a", shlex.quote(approval or "on-request")]
    else:
        parts += [shlex.quote(program)] + [shlex.quote(a) for a in program_args]
    return " ".join(parts)


SPAWN_PANE_FORMAT = ("#{pane_id}\t#{pane_left}\t#{pane_top}\t#{pane_width}"
                     "\t#{pane_height}\t#{window_width}")


def spawn_panes(output):
    panes = []
    for line in output.splitlines():
        fields = line.split("\t")
        if len(fields) != 6:
            continue
        try:
            pane, left, top, width, height, window_width = fields
            panes.append({"pane": pane, "left": int(left), "top": int(top),
                          "width": int(width), "height": int(height),
                          "window_width": int(window_width)})
        except ValueError:
            continue
    return panes


def spawn_target(caller, panes, min_height, vertical=False):
    if vertical:
        return caller, "-v", None
    current = next((pane for pane in panes if pane["pane"] == caller), None)
    if current is None or current["window_width"] < 160:
        return None
    minimum_width = (current["window_width"] + 1) // 2
    if current["width"] < minimum_width:
        return None
    right = [pane for pane in panes
             if pane["pane"] != caller and pane["left"] > current["left"]]
    if not right:
        if len(panes) != 1:
            return None
        if current["height"] < min_height:
            return None
        worker_width = max(1, current["window_width"] - minimum_width - 1)
        return caller, "-h", worker_width
    stack_top = min(pane["top"] for pane in right)
    stack_bottom = max(pane["top"] + pane["height"] for pane in right)
    worker_count = len(right) + 1
    height = (stack_bottom - stack_top - worker_count + 1) // worker_count
    if height < min_height:
        return None
    bottom = max(right, key=lambda pane: (pane["top"], pane["left"]))
    return bottom["pane"], "-v", None


def spawn_split_argv(pane, cwd, line, direction="-h", size=None):
    args = ["split-window", "-d", direction, "-P", "-F", "#{pane_id}"]
    if size is not None:
        args += ["-l", str(size)]
    return tuple(args + ["-t", pane, "-c", cwd, line])


def spawn_window_argv(session, agent_id, cwd, line):
    return ("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", session,
            "-n", agent_id, "-c", cwd, line)


def balance_right_stack(caller):
    output = tmux("list-panes", "-t", caller, "-F", SPAWN_PANE_FORMAT,
                  check=True) or ""
    panes = spawn_panes(output)
    current = next((pane for pane in panes if pane["pane"] == caller), None)
    if current is None:
        return
    right = sorted((pane for pane in panes
                    if pane["pane"] != caller and pane["left"] > current["left"]),
                   key=lambda pane: pane["top"])
    if not right:
        return
    total = sum(pane["height"] for pane in right)
    height, extra = divmod(total, len(right))
    desired = [height + (index < extra) for index in range(len(right))]
    for pane, pane_height in zip(right[:-1], desired[:-1]):
        tmux("resize-pane", "-t", pane["pane"], "-y", str(pane_height), check=True)


def allow_prefixes(values):
    prefixes = []
    for value in values:
        prefix = value.strip()
        if not prefix:
            die("--allow command prefix cannot be empty")
        if prefix not in prefixes:
            prefixes.append(prefix)
    return prefixes


def write_claude_allowlist(cwd, prefixes):
    path = os.path.join(cwd, ".claude", "settings.local.json")
    try:
        with open(path) as fh:
            settings = json.load(fh)
    except FileNotFoundError:
        settings = {}
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read {path}: {exc}")
    if not isinstance(settings, dict):
        die(f"cannot update {path}: root must be a JSON object")
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        die(f"cannot update {path}: permissions must be a JSON object")
    allowed = permissions.setdefault("allow", [])
    if not isinstance(allowed, list):
        die(f"cannot update {path}: permissions.allow must be a JSON array")
    additions = [f"Bash({prefix}:*)" for prefix in prefixes]
    additions = [entry for entry in additions if entry not in allowed]
    if not additions:
        return None
    allowed.extend(additions)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(settings, fh, indent=2)
            fh.write("\n")
    except OSError as exc:
        die(f"cannot write {path}: {exc}")
    return path


def write_codex_allowlist(cwd, prefixes):
    path = os.path.join(cwd, ".codex", "rules", "herdlet.rules")
    rules = []
    for prefix in prefixes:
        try:
            words = shlex.split(prefix)
        except ValueError as exc:
            die(f"invalid --allow command prefix {prefix!r}: {exc}")
        if not words:
            die("--allow command prefix cannot be empty")
        rules.append(f"prefix_rule(pattern={json.dumps(words)}, decision=\"allow\")")
    try:
        with open(path) as fh:
            body = fh.read()
    except FileNotFoundError:
        body = ""
    except OSError as exc:
        die(f"cannot read {path}: {exc}")
    existing = set(body.splitlines())
    additions = [rule for rule in rules if rule not in existing]
    if not additions:
        return None
    updated = body
    if updated and not updated.endswith("\n"):
        updated += "\n"
    updated += "\n".join(additions) + "\n"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(updated)
    except OSError as exc:
        die(f"cannot write {path}: {exc}")
    return path


def write_spawn_allowlist(agent, cwd, values):
    prefixes = allow_prefixes(values)
    if not prefixes:
        return None
    if agent == "claude":
        return write_claude_allowlist(cwd, prefixes)
    return write_codex_allowlist(cwd, prefixes)


def cmd_spawn(args):
    if args.agent not in ("claude", "codex"):
        die("spawn supports claude and codex only; launch other agents by hand (see README)")
    if args.min_height < 1:
        die("--min-height must be one or more")
    sandbox = getattr(args, "sandbox", None)
    approval = getattr(args, "approval", None)
    permission_mode = getattr(args, "permission_mode", None)
    if args.agent == "claude" and sandbox is not None:
        die("--sandbox is only valid with --agent codex")
    if args.agent == "claude" and approval is not None:
        die("--approval is only valid with --agent codex")
    if args.agent == "codex" and permission_mode is not None:
        die("--permission-mode is claude-only; use --sandbox and --approval instead")
    caller = os.environ.get("TMUX_PANE")
    if not caller:
        die("spawn must run inside tmux")
    cwd = os.path.abspath(args.cwd or os.getcwd())
    brief = os.path.abspath(args.brief) if args.brief else None
    title = args.title or (brief_title(brief) if brief else None) or "worker"
    allowlist = write_spawn_allowlist(args.agent, cwd,
                                      getattr(args, "allow", None) or ())
    if allowlist:
        print(f"updated {allowlist}", file=sys.stderr)
    program = getattr(args, "program", None) or args.agent
    line = spawn_line(args.id, args.model, args.effort, title,
                      permission_mode or "auto", args.env or (), program,
                      args.program_args or (), sandbox, approval)

    fallback = None
    placement = "right-stack"
    pane_output = tmux("list-panes", "-t", caller, "-F", SPAWN_PANE_FORMAT,
                       check=True) or ""
    target = spawn_target(caller, spawn_panes(pane_output), args.min_height,
                          args.vertical)
    out = None
    if target is not None:
        target_pane, direction, size = target
        out = tmux_run(*spawn_split_argv(target_pane, cwd, line, direction, size),
                       timeout=15)
        if out is None:
            die("tmux not available")
        if out.returncode != 0 and "no space" not in out.stderr.lower():
            die(f"tmux split-window: {out.stderr.strip()}")
    if target is None or out.returncode != 0:
        session = (tmux("display-message", "-p", "-t", caller,
                        "#{session_name}", check=True) or "").strip()
        fallback = f"window full, opened a new window in session {session}"
        placement = "new-window"
        out = tmux_run(*spawn_window_argv(session, args.id, cwd, line), timeout=15)
        if out is None or out.returncode != 0:
            die(f"tmux new-window: {(out.stderr if out else '').strip()}")
    pane = out.stdout.strip()
    if placement == "right-stack" and not args.vertical:
        balance_right_stack(caller)

    # register before the worker's first hook: until then it has no record at
    # all, so a trust/onboarding prompt is only addressable by raw pane id
    ensure_daemon(args.socket)
    params = {"id": args.id, "agent": args.agent, "pane": pane, "cwd": cwd,
              "model": args.model, "effort": args.effort,
              "message": f"spawning: {title}"}
    if args.agent == "codex" or not spawn_ready(args.socket, args.id, pane):
        params["state"] = "spawning"  # else a hook beat us here; don't undo it
    call_or_die(args.socket, "agent.report", params)
    pair_with_spawner(args.socket, args.id, brief, cwd)

    if args.agent == "codex":
        ready = wait_for_codex_prompt(pane, args.ready_timeout)
    else:
        if args.ready_timeout > 0:
            call_or_die(args.socket, "wait", {
                "id": args.id, "states": ["idle", "working"],
                "timeout_ms": int(args.ready_timeout * 1000),
            }, timeout=args.ready_timeout + 5)
        ready = spawn_ready(args.socket, args.id, pane)
    if ready and brief:
        send_text(pane, f"Read {brief} and do it.")
    # tmux answers a missing -t target with success and an empty line, so the
    # only reliable liveness check is getting the pane id back
    gone = not ready and (tmux("display-message", "-p", "-t", pane,
                               "#{pane_id}") or "").strip() != pane

    if args.json:
        print(json.dumps({"result": {
            "type": "spawned", "id": args.id, "pane": pane, "model": args.model,
            "effort": args.effort, "title": title, "cwd": cwd, "ready": ready,
            "brief_sent": bool(ready and brief), "note": fallback,
            "placement": placement}}, indent=2))
    else:
        if fallback:
            print(fallback)
        print(f"spawned {args.id} in {pane} ({args.model}/{args.effort})")
    if gone:
        print(f"herdlet: pane {pane} is gone: the launch command exited; "
              f"check the model/effort flags", file=sys.stderr)
        return 1
    if not ready:
        brief_note = ("; the brief was not sent, send it once the prompt is "
                      "cleared" if brief else "")
        wait_note = ("did not show its input prompt" if args.agent == "codex"
                     else "did not report")
        print(f"warning: {args.id} {wait_note} in {args.ready_timeout}s; "
              f"pane {pane} is alive, peek/approve it by pane id{brief_note}",
              file=sys.stderr)
    return 0


def spawn_topic(agent_id, cwd):
    return os.path.join(cwd, "plans", f"{agent_id.replace('/', '-')}-thread.md")


def pair_with_spawner(sock_path, agent_id, brief, cwd):
    """You may talk to what you spawned - downward only.

    A nested master is a worker to the scope check (spawn gives it a
    HERDLET_ID), so without this its own children would be out of reach. The
    link is one-directional on purpose: a child must not get a shortcut into
    its master's pane. Best effort - an unregistered spawner has no HERDLET_ID
    either, so it is unrestricted anyway.
    """
    spawner = default_id()
    if not spawner or spawner == agent_id:
        return None
    topic = brief or spawn_topic(agent_id, cwd)
    resp = call_or_die(sock_path, "agent.pair",
                       {"id": spawner, "with": agent_id, "topic": topic,
                        "oneway": True})
    return topic if "result" in resp else None


def spawn_ready(sock_path, agent_id, pane):
    # the pane check is what stops a leftover idle record under the same id (an
    # acked worker, a record still inside MAX_AGE) from passing as this worker
    rec = call_or_die(sock_path, "agent.get", {"id": agent_id}).get("result") or {}
    return rec.get("state") in ("idle", "working") and rec.get("pane") == pane


def codex_prompt_ready(text):
    return bool(re.search(
        r"^\s*(?:›\s*)?Ask Codex to do anything\s*$", text, re.I | re.M))


def codex_trust_prompt(text):
    return (bool(re.search(r"^\s*(?:›\s*)?1\.\s*Yes, continue\s*$", text,
                           re.I | re.M))
            and bool(re.search(r"^\s*2\.\s*No, quit\s*$", text, re.I | re.M)))


def wait_for_codex_prompt(pane, timeout):
    deadline = time.monotonic() + max(0, timeout)
    trust_answered = False
    while True:
        text = tmux("capture-pane", "-p", "-t", pane) or ""
        if codex_prompt_ready(text):
            return True
        if not trust_answered and codex_trust_prompt(text):
            tmux("send-keys", "-t", pane, "Enter", check=True)
            trust_answered = True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))


def pane_has_menu(capture):
    if codex_trust_prompt(capture):
        return True
    choices = re.findall(r"^\s*[❯›>]?\s*\d+\.\s+\S", capture, re.M)
    controls = re.search(r"(?:esc to cancel|enter to confirm|\(esc\))", capture, re.I)
    return len(choices) >= 2 and bool(controls)


def menu_options(capture):
    options = []
    current = None
    for line in capture.splitlines():
        match = re.match(r"^\s*[❯›>]?\s*(\d+)\.\s+(\S.*)\s*$", line)
        if match:
            if current:
                options.append((current[0], " ".join(current[1])))
            current = (match.group(1), [match.group(2).strip()])
            continue
        if current:
            text = line.strip()
            if re.search(r"(?:esc to cancel|enter to confirm)", text, re.I):
                options.append((current[0], " ".join(current[1])))
                current = None
            elif text and not re.fullmatch(r"[─━]+", text):
                current[1].append(text)
    if current:
        options.append((current[0], " ".join(current[1])))
    return options


def approve_choice(capture, choice):
    options = menu_options(capture)
    if codex_trust_prompt(capture):
        number = "2" if choice == "no" else "1"
        return number, choice, False

    def normalized(text):
        return text.lower().replace("’", "'")

    def is_always(text):
        text = normalized(text)
        return (not text.startswith("no")
                and ("don't ask again" in text or "do not ask again" in text
                     or "and don't ask" in text or "always" in text))

    if choice == "always":
        match = next((number for number, text in options if is_always(text)), None)
        if match:
            return match, "always", False
        choice = "yes"
        fallback = True
    else:
        fallback = False
    if choice == "yes":
        match = next((number for number, text in options
                      if normalized(text).startswith("yes") and not is_always(text)), None)
    else:
        match = next((number for number, text in options
                      if normalized(text).startswith("no")), None)
    return match, choice, fallback


def pane_tail(capture, lines=5):
    return [line.strip() for line in capture.splitlines() if line.strip()][-lines:]


def cmd_ack(args):
    ids = [s.strip() for s in args.id.split(",") if s.strip()]
    missing = 0
    for agent_id in ids:
        resp = call_or_die(args.socket, "agent.get", {"id": agent_id})
        rec = resp.get("result")
        if rec is None:
            print(f"{agent_id}: unknown agent", file=sys.stderr)
            missing += 1  # keep going: one retired worker must not block the rest
            continue
        if rec["state"] == "done":
            call_or_die(args.socket, "agent.report", {"id": agent_id, "state": "idle"})
            print(f"{agent_id}: done -> idle")
            if args.kill_pane:
                kill_record_pane(agent_id, rec)
        elif rec["state"] == "ended":
            # dead + collected: clear it so `list` stays an inbox of live work
            if args.kill_pane:
                kill_record_pane(agent_id, rec)
            call_or_die(args.socket, "agent.remove", {"id": agent_id})
            print(f"{agent_id}: ended -> removed")
        else:
            print(f"{agent_id}: {rec['state']} (nothing to ack)")
            if args.kill_pane:
                kill_record_pane(agent_id, rec)
    return 1 if missing else 0


def cmd_resume(args):
    resp = call_or_die(args.socket, "agent.get", {"id": args.id})
    rec = resp.get("result")
    if rec is None:
        die(f"unknown agent '{args.id}' (see: herdlet list)")
    session = rec.get("session")
    if not session:
        die(f"no session recorded for '{args.id}'; its hooks never reported one")
    if not re.fullmatch(r"[A-Za-z0-9_./:-]{1,200}", session):
        die("recorded session ref looks unsafe; refusing to type it")
    agent = args.agent or rec.get("agent") or "claude"
    template = RESUME.get(agent)
    if not template:
        die(f"no resume syntax known for agent '{agent}' (known: {', '.join(sorted(RESUME))})")
    pane = args.pane or rec.get("pane")
    if not pane:
        die(f"'{args.id}' has no pane; spawn one and pass --pane %N")
    current = (tmux("display-message", "-p", "-t", pane, "#{pane_current_command}") or "").strip()
    if not args.force and current and current not in SHELLS:
        die(f"pane {pane} is running '{current}', not a bare shell; pass --force to type anyway")
    cmd = template.format(session=shlex.quote(session))
    tmux("send-keys", "-t", pane, "-l", "--", cmd, check=True)
    time.sleep(0.2)
    tmux("send-keys", "-t", pane, "Enter", check=True)
    print(f"resume sent to {pane}: {cmd}")


def cmd_approve(args):
    if args.option is not None and not (
            len(args.option) == 1 and args.option.isdigit()):
        die("--option must be a single digit menu key")
    pane = resolve_pane(args.socket, args.id)
    capture = tmux("capture-pane", "-p", "-J", "-t", pane, check=True) or ""
    if not pane_has_menu(capture):
        try:
            record = call(args.socket, "agent.get", {"id": args.id}).get("result")
            if record and record.get("state") == "blocked":
                call(args.socket, "agent.report",
                     {"id": args.id, "state": "working", "message": ""}, timeout=1.0)
        except OSError:
            pass
        print(f"herdlet: no menu on {args.id}; nothing typed", file=sys.stderr)
        for line in pane_tail(capture):
            print(line, file=sys.stderr)
        return 5
    option = args.option
    approved = None
    if option is None:
        option, approved, fallback = approve_choice(capture, args.choice)
        if fallback:
            print("herdlet: no \"don't ask again\" option on this menu; chose Yes",
                  file=sys.stderr)
        if option is None:
            print(f"herdlet: no {args.choice} option on this menu; nothing typed",
                  file=sys.stderr)
            for line in pane_tail(capture):
                print(line, file=sys.stderr)
            return 5
    tmux("send-keys", "-t", pane, option, check=True)  # bare keypress: menus react without Enter
    report_failed = False
    try:
        record = call(args.socket, "agent.get", {"id": args.id}).get("result")
        edge = record is not None
        if edge:
            call(args.socket, "agent.report",
                 {"id": args.id, "state": "working",
                  "message": (f"approved {approved} (option {option})" if approved
                              else f"approved option {option}")}, timeout=1.0)
    except OSError:
        edge, report_failed = False, True
    time.sleep(args.settle)

    if not args.wait:
        out = tmux("capture-pane", "-p", "-t", pane, "-S", f"-{args.lines}", check=True)
        print(out.rstrip("\n"))
        return 0

    if report_failed:
        # approve's core job (the keypress) already happened; don't fail after the side effect
        out = tmux("capture-pane", "-p", "-t", pane, "-S", f"-{args.lines}", check=True)
        print(out.rstrip("\n"))
        return 0

    states = [s.strip() for s in args.state.split(",") if s.strip()]
    params = {"id": args.id, "states": states}
    if edge:
        params["edge"] = True
    if args.timeout:
        params["timeout_ms"] = int(args.timeout * 1000)
    client_timeout = args.timeout + 5 if args.timeout else None
    hung = False
    try:
        resp = call(args.socket, "wait", params, timeout=client_timeout)
        state = resp["result"]["state"] if "result" in resp else "timeout"
    except TimeoutError:  # hung daemon: still a failure, whatever --timeout-ok says
        state, hung = "timeout", True
    except OSError:
        out = tmux("capture-pane", "-p", "-t", pane, "-S", f"-{args.lines}", check=True)
        print(out.rstrip("\n"))
        return 0

    out = tmux("capture-pane", "-p", "-t", pane, "-S", f"-{args.lines}", check=True)
    print(f"state: {state}")
    print(out.rstrip("\n"))
    if state != "timeout":
        return 0
    return 0 if args.timeout_ok and not hung else 2


COLORS = {"working": "\033[33m", "blocked": "\033[1;31m", "done": "\033[32m",
          "idle": "\033[2m", "unknown": "\033[2m", "gone": "\033[35m",
          "stale": "\033[31m", "ended": "\033[2m", "limited": "\033[1;35m",
          "spawning": "\033[36m"}
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"


def clip(text, width):
    if len(text) <= width:
        return text
    return text[:max(0, width - 1)] + "…"


def draw(agents, note):
    cols, lines = shutil.get_terminal_size()
    out = ["\033[2J\033[H"]
    out.append(f" {BOLD}herdlet{RESET} {DIM}· {len(agents)} agent(s) · "
               f"{time.strftime('%H:%M:%S')}{RESET}\n\n")
    if not agents:
        out.append(f"  {DIM}no agents registered yet{RESET}\n")
    id_w = min(max([len(r["id"]) for r in agents] + [2]), max(8, cols // 4))
    where_w = max([len(r["where"]) for r in agents] + [0])
    fixed = id_w + 19  # margin + index + dot + state + age + the gaps between
    # narrow screen (phone popup): where is the first column to go, message the last
    show_where = where_w and cols - fixed - (where_w + 1) >= 16
    msg_w = cols - fixed - ((where_w + 1) if show_where else 0)
    for i, rec in enumerate(agents[:min(9, lines - 5)]):
        color = COLORS.get(rec["state"], "")
        parts = [
            f"  {DIM}{i + 1}{RESET}",
            f"{color}●{RESET}",
            clip(rec["id"], id_w).ljust(id_w),
            f"{color}{rec['state'].ljust(7)}{RESET}",
            rec["age"].rjust(3),
        ]
        if show_where:
            parts.append(f"{DIM}{clip(rec['where'], where_w).ljust(where_w)}{RESET}")
        if msg_w >= 4:
            parts.append(clip(rec.get("message") or "", msg_w))
        out.append(" ".join(parts).rstrip() + "\n")
    hint = "q or esc quit · 1-9 jump to pane" if cols >= 46 else "q/esc quit · 1-9 jump"
    out.append(f"\n {DIM}{hint}{RESET}")
    if note:
        out.append(f"  {COLORS['blocked']}{note}{RESET}")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def jump(rec, panes):
    pane = rec.get("pane")
    info = panes.get(pane) if pane else None
    if not info:
        return False
    tmux("switch-client", "-t", info["session"])
    tmux("select-window", "-t", info["window_id"])
    tmux("select-pane", "-t", pane)
    return True


def cmd_monitor(args):
    import termios
    import tty

    if not sys.stdin.isatty():
        die("monitor needs a tty (run it in a pane or tmux popup)")
    ensure_daemon(args.socket)
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    sys.stdout.write("\033[?1049h\033[?25l")
    try:
        tty.setcbreak(fd)
        while True:
            if not _monitor_session(args.socket, fd, args.session, args.prefix):
                return 0
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()


QUIT_KEYS = ("q", "Q", "\x1b", "\x03")  # esc also catches arrow-key prefixes; fine, monitor has no arrows


def _quit_pressed(stdin_fd, timeout):
    import selectors

    sel = selectors.DefaultSelector()
    sel.register(stdin_fd, selectors.EVENT_READ)
    ready = sel.select(timeout=timeout)
    sel.close()
    if not ready:
        return False
    return os.read(stdin_fd, 1).decode(errors="replace") in QUIT_KEYS


def _monitor_session(sock_path, stdin_fd, session=None, prefix=None):
    """One connected stretch. Returns False to quit, True to reconnect."""
    import selectors

    try:
        conn = socket.socket(socket.AF_UNIX)
        conn.connect(sock_path)
        conn.setblocking(False)
        conn.send((json.dumps({"id": "m", "method": "subscribe", "params": {}}) + "\n").encode())
    except OSError:
        draw([], "daemon not running, retrying...")
        return not _quit_pressed(stdin_fd, 1.0)  # keep keys live while down, and pace the retry

    sel = selectors.DefaultSelector()
    sel.register(conn, selectors.EVENT_READ)
    sel.register(stdin_fd, selectors.EVENT_READ)
    agents = []

    def refresh():
        nonlocal agents
        try:
            resp = call(sock_path, "agent.list", {}, timeout=2.0)
            agents = filter_agents(
                annotate(resp.get("result", {}).get("agents", []), pane_map()),
                session, prefix=prefix)
        except OSError:
            pass
        draw(agents, "")

    refresh()
    try:
        while True:
            events = sel.select(timeout=1.0)
            dirty = not events  # tick: ages move even when nothing happened
            for key, _ in events:
                if key.fileobj == stdin_fd:
                    ch = os.read(stdin_fd, 1).decode(errors="replace")
                    if ch in QUIT_KEYS:
                        return False
                    if ch.isdigit() and 0 < int(ch) <= len(agents):
                        if jump(agents[int(ch) - 1], pane_map()):
                            return False
                else:
                    if not conn.recv(4096):
                        return True  # daemon went away
                    dirty = True
            if dirty:
                refresh()
    finally:
        sel.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        prog="herdlet", description="tiny coordination bus for coding agents in tmux panes")
    parser.add_argument("--socket", default=DEFAULT_SOCK, help="unix socket path")
    parser.add_argument("--version", action="version", version=f"herdlet {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the coordination daemon")
    p.add_argument("--if-needed", action="store_true", help="exit 0 if already running")
    p.set_defaults(fn=cmd_serve)

    p = sub.add_parser("ping", help="check the daemon")
    p.set_defaults(fn=cmd_ping)

    p = sub.add_parser("report", help="report an agent's state")
    p.add_argument("--id", help="agent id (default: $HERDLET_ID or $TMUX_PANE)")
    p.add_argument("--state", required=True, help=f"one of {'/'.join(STATES)} or custom")
    p.add_argument("--message", help="what the agent is doing ('' clears)")
    p.add_argument("--agent", help="agent kind, e.g. claude / codex")
    p.add_argument("--pane", help="tmux pane id (default: $TMUX_PANE)")
    p.add_argument("--cwd", help="working directory")
    p.add_argument("--session", help="agent's native session ref (enables resume)")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("get", help="get one agent's record")
    p.add_argument("--id", help="agent id (default: $HERDLET_ID or $TMUX_PANE)")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("list", help="list registered agents")
    p.add_argument("--json", action="store_true")
    p.add_argument("--session", help="only agents in this tmux session")
    p.add_argument("--here", action="store_true", help="only agents in the current tmux session")
    p.add_argument("--prefix", help="only ids starting with this prefix, e.g. myproject/")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("pair", help="let two agents talk to each other directly, on one topic")
    p.add_argument("--id", required=True, help="one agent id")
    p.add_argument("--with", dest="peer", required=True, help="the other agent id")
    p.add_argument("--topic", required=True,
                   help="topic file the exchange is logged to (the master reads it later)")
    p.set_defaults(fn=cmd_pair)

    p = sub.add_parser("unpair", help="remove a peer link from both agents")
    p.add_argument("--id", required=True)
    p.add_argument("--with", dest="peer", required=True)
    p.set_defaults(fn=cmd_unpair)

    p = sub.add_parser("remove", help="remove an agent from the registry")
    p.add_argument("--id", help="agent id (default: $HERDLET_ID or $TMUX_PANE)")
    p.add_argument("--kill-pane", action="store_true",
                   help="also kill a finished agent pane or a pane at a shell")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("wait", help="block until an agent reaches a state, or its pane output matches")
    p.add_argument("--id", help="agent id(s), comma-separated: wakes on whichever transitions first")
    p.add_argument("--prefix", help="also wake on any agent whose id starts with this prefix")
    p.add_argument("--state", help="target state(s), comma-separated")
    p.add_argument("--match", help="instead: regex to match against the pane's recent output")
    p.add_argument("--lines", type=int, default=200, help="output lines scanned per --match poll")
    p.add_argument("--timeout", type=float, help="seconds (exit 2 on timeout)")
    p.add_argument("--edge", action="store_true",
                   help="ignore the current state; wake only on a fresh report "
                        "(use right after answering a menu, to avoid matching the stale state)")
    p.add_argument("--on-compact", action="store_true",
                   help="also wake on the target's next context compaction")
    p.add_argument("--timeout-ok", action="store_true",
                   help="a timeout is a result, not an error: exit 0 with "
                        "result.type 'timeout' (for harnesses that read a "
                        "background command's exit code as failure)")
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("watch", help="stream state-change events as JSON lines")
    p.add_argument("--id")
    p.add_argument("--state")
    p.set_defaults(fn=cmd_watch)

    p = sub.add_parser("hook", help="adapter for Claude Code / Codex hooks (reads stdin JSON)")
    p.add_argument("--agent", default="claude", help="claude / codex (default: claude)")
    p.add_argument("--event", help="override hook_event_name")
    p.add_argument("--id", help="override agent id")
    p.set_defaults(fn=cmd_hook)

    p = sub.add_parser("send", help="type text into an agent's pane (submits with Enter)")
    p.add_argument("--id", required=True, help="agent id or tmux pane id")
    p.add_argument("--no-enter", action="store_true")
    p.add_argument("--settle", type=float, default=5,
                   help="seconds to wait for prompt changes (default: 5)")
    p.add_argument("--ack", action="store_true",
                   help="wait for the target hook to record the submitted prompt")
    p.add_argument("--no-verify", action="store_true",
                   help="send without prompt checks (required with --no-enter)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--file", help="read the message from PATH ('-' for stdin) "
                                  "instead of the positional text")
    p.add_argument("text", nargs="*")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("peek", help="read an agent's recent pane output")
    p.add_argument("--id", required=True, help="agent id or tmux pane id")
    p.add_argument("--lines", type=int, default=None,
                   help="pane lines (default 60), or assistant messages with "
                        "--transcript (default 1)")
    p.add_argument("--join", action="store_true", help="unwrap soft-wrapped lines (better for logs)")
    p.add_argument("--transcript", action="store_true",
                   help="read the agent's own transcript jsonl instead of the "
                        "pane: the last --lines assistant messages, verbatim")
    p.set_defaults(fn=cmd_peek)

    p = sub.add_parser("spawn", help="launch a Claude Code or Codex worker in a new pane and register it")
    p.add_argument("--id", required=True, help="agent id, e.g. myproject/dev")
    p.add_argument("--model", required=True, help="model id (never inherit the default)")
    p.add_argument("--effort", required=True,
                   choices=("low", "medium", "high", "xhigh"))
    p.add_argument("--agent", default="claude", help="agent kind: claude or codex (default: claude)")
    p.add_argument("--title", help="one-line purpose (default: the brief's first heading)")
    p.add_argument("--brief", help="brief file; the worker is told to read it and do it")
    p.add_argument("--cwd", help="worker's working directory (default: yours)")
    p.add_argument("--permission-mode", help="Claude permission mode (default: auto)")
    p.add_argument("--sandbox", choices=("read-only", "workspace-write", "danger-full-access"),
                   help="Codex sandbox mode (default: workspace-write)")
    p.add_argument("--approval", choices=("on-request", "never"),
                   help="Codex approval policy (default: on-request)")
    p.add_argument("--allow", action="append", metavar="COMMAND_PREFIX",
                   help="pre-allow a command prefix in the worker cwd (repeatable)")
    p.add_argument("--env", action="append", metavar="K=V",
                   help="extra env var for the worker (repeatable)")
    p.add_argument("--vertical", action="store_true", help="split vertically")
    p.add_argument("--min-height", type=int, default=12,
                   help="minimum rows per right-stack worker (default: 12)")
    p.add_argument("--ready-timeout", type=float, default=30,
                   help="seconds to wait for worker readiness (default: 30)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--program", help=argparse.SUPPRESS)
    p.add_argument("--program-args", action="append", help=argparse.SUPPRESS)
    p.set_defaults(fn=cmd_spawn)

    p = sub.add_parser("ack", help="mark collected results as seen: done -> idle")
    p.add_argument("--id", required=True, help="agent id(s), comma-separated")
    p.add_argument("--kill-pane", action="store_true",
                   help="also kill a finished agent pane or a pane at a shell")
    p.set_defaults(fn=cmd_ack)

    p = sub.add_parser("resume", help="type an agent's native resume command into its pane")
    p.add_argument("--id", required=True, help="agent id (session ref comes from its hook reports)")
    p.add_argument("--pane", help="target pane override, e.g. %%7 (default: the recorded pane)")
    p.add_argument("--agent", help="agent kind override (default: recorded kind, else claude)")
    p.add_argument("--force", action="store_true",
                   help="type even if the pane is not sitting at a bare shell")
    p.set_defaults(fn=cmd_resume)

    p = sub.add_parser("approve", help="answer an agent's numbered permission menu, then show its pane")
    p.add_argument("--id", required=True, help="agent id or tmux pane id")
    choice = p.add_mutually_exclusive_group()
    choice.add_argument("--choice", choices=("yes", "always", "no"), default="yes",
                        help="select by option text (default: yes)")
    choice.add_argument("--option", help="unsafe raw menu option digit")
    p.add_argument("--lines", type=int, default=20, help="pane lines to echo back after answering")
    p.add_argument("--settle", type=float, default=1.0,
                   help="seconds to let the TUI redraw before reading (default: 1.0)")
    p.add_argument("--wait", action="store_true",
                   help="after answering, mark the agent working and wait for its next "
                        "real transition (see --state/--timeout below), then show the pane")
    p.add_argument("--state", default="done,blocked",
                   help="target state(s) for --wait, comma-separated (default: done,blocked)")
    p.add_argument("--timeout", type=float, default=550,
                   help="seconds for --wait (default: 550)")
    p.add_argument("--timeout-ok", action="store_true",
                   help="exit 0 instead of 2 when --wait times out")
    p.set_defaults(fn=cmd_approve)

    p = sub.add_parser("monitor", help="live status view (made for a tmux popup)")
    p.add_argument("--session", help="only agents in this tmux session")
    p.add_argument("--prefix", help="only ids starting with this prefix")
    p.set_defaults(fn=cmd_monitor)

    p = sub.add_parser("setup", help="wire Claude Code / Codex hooks, permissions and the skill")
    p.add_argument("--allow-tmux", action="store_true",
                   help="also allow Bash(tmux:*) so agents can spawn worker panes unprompted")
    p.set_defaults(fn=cmd_setup)

    args = parser.parse_args()
    # not for serve (it IS the daemon) and not for hook (which must never print,
    # and would pay for a ping on every tool call)
    if args.cmd not in ("serve", "hook") and daemon_running(args.socket):
        warn_version_skew(args.socket)
    try:
        sys.exit(args.fn(args) or 0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
