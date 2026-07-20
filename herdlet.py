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

__version__ = "0.6.0"

DEFAULT_SOCK = os.environ.get("HERDLET_SOCKET", os.path.expanduser("~/.herdlet.sock"))
LOG_PATH = os.path.expanduser("~/.herdlet.log")
STATES = ("idle", "working", "blocked", "done", "ended", "unknown")
TERMINAL = ("done", "ended")  # agent's turn/session finished; record kept for collection + resume
MERGE_KEYS = ("message", "agent", "pane", "cwd", "session")
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
        # Terminal records persist across restarts (so resume survives one), but
        # drop the long-dead ones so the registry doesn't grow without bound.
        cutoff = time.time() - TERMINAL_TTL
        self.agents = {
            aid: rec for aid, rec in self.agents.items()
            if not (rec.get("state") in TERMINAL and rec.get("updated", 0) < cutoff)
        }
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
        return {"id": agent_id, **rec} if rec else None

    def report(self, agent_id, params):
        rec = self.agents.setdefault(agent_id, {
            "state": "unknown", "message": None, "agent": None,
            "pane": None, "cwd": None, "session": None, "updated": 0.0,
        })
        state = params.get("state") or rec["state"]
        rec["state"] = state
        for key in MERGE_KEYS:
            value = params.get(key)
            if value == "":
                rec[key] = None  # explicit clear; absent/null means preserve
            elif value is not None:
                rec[key] = value
        rec["updated"] = round(time.time(), 3)
        self._save()
        event = {"type": "agent.state_changed", **self.snapshot(agent_id)}
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
        self._save()
        event = {"type": "agent.removed", "id": agent_id}
        self._fanout(event, agent_id, None)
        return event

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
            if predicate(agent_id, state):
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


def _send(writer, obj):
    writer.write((json.dumps(obj) + "\n").encode())


def _log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


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
    if (not ids and not prefix) or not states:
        _send(writer, {"id": rid, "error": {"code": "invalid_params"}})
        return

    def matches(agent_id):
        return agent_id in ids or (prefix is not None and agent_id.startswith(prefix))

    def matched_now():
        # every currently-matching agent already in a target state, so a herd
        # wait can batch-collect them instead of re-issuing the wait per straggler
        return [bus.snapshot(a) for a in bus.agents
                if matches(a) and bus.agents[a]["state"] in states]

    if not params.get("edge"):
        ready = matched_now()
        if ready:
            _send(writer, {"id": rid, "result": {
                **ready[0], "type": "waited", "already": True, "matched": ready}})
            return

    fut = asyncio.get_running_loop().create_future()
    entry = (lambda i, s: matches(i) and s in states, fut)
    bus.waiters.append(entry)
    timeout = params.get("timeout_ms")
    try:
        event = await asyncio.wait_for(fut, timeout / 1000.0 if timeout else None)
        _send(writer, {"id": rid, "result": {
            **event, "type": "waited", "already": False, "matched": matched_now()}})
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


async def _serve(sock_path):
    bus = Bus(state_path=sock_path + ".state")
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


def tmux(*args, check=False, input=None):
    try:
        out = subprocess.run(("tmux",) + args, capture_output=True, text=True,
                             timeout=5, input=input)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        if check:
            die("tmux not available")
        return None
    if out.returncode != 0:
        if check:
            die(f"tmux {' '.join(args)}: {out.stderr.strip()}")
        return None
    return out.stdout


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


def cmd_remove(args):
    agent_id = args.id or default_id()
    if not agent_id:
        die("no agent id: pass --id, or set HERDLET_ID, or run inside tmux")
    emit(call_or_die(args.socket, "agent.remove", {"id": agent_id}))


def sort_key(rec):
    order = {"blocked": 0, "stale": 1, "gone": 2, "working": 3, "done": 4,
             "ended": 5, "idle": 6}
    return (order.get(rec["state"], 7), -rec["updated"])


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
        if (info and rec["state"] in ("working", "blocked")
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
    rows = [("ID", "STATE", "AGE", "AGENT", "PANE", "WHERE", "MESSAGE")]
    for rec in agents:
        rows.append((rec["id"], rec["state"], rec["age"], rec.get("agent") or "-",
                     rec.get("pane") or "-", rec["where"], rec.get("message") or ""))
    widths = [max(len(row[i]) for row in rows) for i in range(6)]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:6])) + "  " + row[6])


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
            print(json.dumps({"error": {"code": "timeout", "id": ids[0],
                                        "match": args.match}}, indent=2))
            return 2
        time.sleep(2)


def cmd_wait(args):
    ids = [s.strip() for s in (args.id or "").split(",") if s.strip()]
    if not ids and not args.prefix:
        die("pass --id (comma-separated waits on whichever transitions first) and/or --prefix")
    if args.edge and args.match:
        die("--edge is meaningless with --match: a match poll never checks stored state")
    if args.match:
        return wait_for_match(args, ids)
    if not args.state:
        die("pass --state (or --match to wait on pane output)")
    states = [s.strip() for s in args.state.split(",") if s.strip()]
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
    if args.timeout:
        params["timeout_ms"] = int(args.timeout * 1000)
    client_timeout = args.timeout + 5 if args.timeout else None
    try:
        emit(call(args.socket, "wait", params, timeout=client_timeout))
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
                 "PostToolUse", "Notification", "Stop")
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


def cmd_send(args):
    pane = resolve_pane(args.socket, args.id)
    text = " ".join(args.text)
    if "\n" in text:
        # bracketed paste: a readline TUI takes the newlines as text, not submits
        tmux("load-buffer", "-b", "herdlet-send", "-", input=text, check=True)
        tmux("paste-buffer", "-p", "-d", "-b", "herdlet-send", "-t", pane, check=True)
    else:
        tmux("send-keys", "-t", pane, "-l", "--", text, check=True)
    if not args.no_enter:
        time.sleep(0.2)  # let the TUI ingest the text before submit
        tmux("send-keys", "-t", pane, "Enter", check=True)


def cmd_peek(args):
    pane = resolve_pane(args.socket, args.id)
    flags = ["-p", "-J"] if args.join else ["-p"]
    out = tmux("capture-pane", *flags, "-t", pane, "-S", f"-{args.lines}", check=True)
    print(out.rstrip("\n"))


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
        elif rec["state"] == "ended":
            # dead + collected: clear it so `list` stays an inbox of live work
            call_or_die(args.socket, "agent.remove", {"id": agent_id})
            print(f"{agent_id}: ended -> removed")
        else:
            print(f"{agent_id}: {rec['state']} (nothing to ack)")
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
    if not (len(args.option) == 1 and args.option.isdigit()):
        die("--option must be a single digit menu key")
    pane = resolve_pane(args.socket, args.id)
    tmux("send-keys", "-t", pane, args.option, check=True)  # bare keypress: menus react without Enter
    time.sleep(args.settle)

    if not args.wait:
        out = tmux("capture-pane", "-p", "-t", pane, "-S", f"-{args.lines}", check=True)
        print(out.rstrip("\n"))
        return 0

    try:
        record = call(args.socket, "agent.get", {"id": args.id}).get("result")
        # only report working if the record still shows the stale pre-answer
        # 'blocked'; a state that already moved on (settle-window race) must
        # be seen by a plain (non-edge) wait below, not skipped past
        edge = record is not None and record.get("state") == "blocked"
        if edge:
            # answering a menu resumes the turn either way (approve or deny);
            # the next real hook event corrects this if the optimism is wrong
            call(args.socket, "agent.report",
                 {"id": args.id, "state": "working", "message": ""}, timeout=1.0)
    except OSError:
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
    try:
        resp = call(args.socket, "wait", params, timeout=client_timeout)
        state = resp["result"]["state"] if "result" in resp else "timeout"
    except TimeoutError:  # hung daemon: treat like a normal wait timeout, not unreachable
        state = "timeout"
    except OSError:
        out = tmux("capture-pane", "-p", "-t", pane, "-S", f"-{args.lines}", check=True)
        print(out.rstrip("\n"))
        return 0

    out = tmux("capture-pane", "-p", "-t", pane, "-S", f"-{args.lines}", check=True)
    print(f"state: {state}")
    print(out.rstrip("\n"))
    return 0 if state != "timeout" else 2


COLORS = {"working": "\033[33m", "blocked": "\033[1;31m", "done": "\033[32m",
          "idle": "\033[2m", "unknown": "\033[2m", "gone": "\033[35m",
          "stale": "\033[31m", "ended": "\033[2m"}
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

    p = sub.add_parser("remove", help="remove an agent from the registry")
    p.add_argument("--id", help="agent id (default: $HERDLET_ID or $TMUX_PANE)")
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
    p.add_argument("text", nargs="+")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("peek", help="read an agent's recent pane output")
    p.add_argument("--id", required=True, help="agent id or tmux pane id")
    p.add_argument("--lines", type=int, default=60)
    p.add_argument("--join", action="store_true", help="unwrap soft-wrapped lines (better for logs)")
    p.set_defaults(fn=cmd_peek)

    p = sub.add_parser("ack", help="mark collected results as seen: done -> idle")
    p.add_argument("--id", required=True, help="agent id(s), comma-separated")
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
    p.add_argument("--option", default="1", help="menu option key to press (default: 1)")
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
    try:
        sys.exit(args.fn(args) or 0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
