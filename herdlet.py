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
import shutil
import signal
import socket
import subprocess
import sys
import time

__version__ = "0.1.0"

DEFAULT_SOCK = os.environ.get("HERDLET_SOCKET", os.path.expanduser("~/.herdlet.sock"))
LOG_PATH = os.path.expanduser("~/.herdlet.log")
STATES = ("idle", "working", "blocked", "done", "unknown")
MERGE_KEYS = ("message", "agent", "pane", "cwd")


# --------------------------------------------------------------------------
# daemon
# --------------------------------------------------------------------------

class Bus:
    def __init__(self):
        self.agents = {}       # id -> record
        self.subscribers = set()  # (queue, id_filter, state_filter)
        self.waiters = []      # (predicate, future)

    def snapshot(self, agent_id):
        rec = self.agents.get(agent_id)
        return {"id": agent_id, **rec} if rec else None

    def report(self, agent_id, params):
        rec = self.agents.setdefault(agent_id, {
            "state": "unknown", "message": None, "agent": None,
            "pane": None, "cwd": None, "updated": 0.0,
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
        event = {"type": "agent.state_changed", **self.snapshot(agent_id)}
        self._fanout(event, agent_id, state)
        self._wake(agent_id, state, event)
        return event

    def remove(self, agent_id):
        rec = self.agents.pop(agent_id, None)
        if rec is None:
            return None
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
                _send(writer, {"id": rid, "result": {"type": "reported", **event}})

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
                    _send(writer, {"id": rid, "result": {"type": "removed", **event}})

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
    agent_id = params.get("id")
    states = params.get("states") or ([params["state"]] if params.get("state") else None)
    if not agent_id or not states:
        _send(writer, {"id": rid, "error": {"code": "invalid_params"}})
        return

    snap = bus.snapshot(agent_id)
    if snap and snap["state"] in states:
        _send(writer, {"id": rid, "result": {"type": "waited", "already": True, **snap}})
        return

    fut = asyncio.get_running_loop().create_future()
    entry = (lambda i, s: i == agent_id and s in states, fut)
    bus.waiters.append(entry)
    timeout = params.get("timeout_ms")
    try:
        event = await asyncio.wait_for(fut, timeout / 1000.0 if timeout else None)
        _send(writer, {"id": rid, "result": {"type": "waited", "already": False, **event}})
    except asyncio.TimeoutError:
        _send(writer, {"id": rid, "error": {"code": "timeout", "agent": agent_id, "states": states}})
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
    bus = Bus()
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


# --------------------------------------------------------------------------
# client plumbing
# --------------------------------------------------------------------------

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


# --------------------------------------------------------------------------
# tmux glue
# --------------------------------------------------------------------------

def tmux(*args, check=False):
    try:
        out = subprocess.run(("tmux",) + args, capture_output=True, text=True, timeout=5)
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
               "#{pane_id}\t#{session_name}\t#{window_id}\t#{window_index}\t#{window_name}")
    panes = {}
    for line in (out or "").splitlines():
        pane, session, window_id, window_index, window_name = (line.split("\t") + [""] * 5)[:5]
        panes[pane] = {"session": session, "window_id": window_id,
                       "window_index": window_index, "window_name": window_name}
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


# --------------------------------------------------------------------------
# subcommands
# --------------------------------------------------------------------------

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
    order = {"blocked": 0, "working": 1, "done": 2, "idle": 3}
    return (order.get(rec["state"], 4), -rec["updated"])


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
        rec["where"] = f"{info['session']}:{info['window_index']} {info['window_name']}" if info else ""
        rec["age"] = age(now - rec["updated"])
    agents.sort(key=sort_key)
    return agents


def cmd_list(args):
    resp = call_or_die(args.socket, "agent.list", {})
    agents = resp.get("result", {}).get("agents", [])
    if args.json:
        print(json.dumps(agents, indent=2))
        return
    agents = annotate(agents, pane_map())
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


def cmd_wait(args):
    agent_id = args.id or default_id()
    states = [s.strip() for s in args.state.split(",") if s.strip()]
    params = {"id": agent_id, "states": states}
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


def cmd_hook(args):
    # fired from agent hook chains: never block, never fail, never print
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
            call(args.socket, "agent.remove", {"id": agent_id}, timeout=1.0)
            return 0
        state = HOOK_STATES.get(event)
        if state is None:
            return 0

        params = {"id": agent_id, "state": state,
                  "agent": args.agent,
                  "pane": os.environ.get("TMUX_PANE"),
                  "cwd": data.get("cwd") or os.getcwd()}
        # message: prompt/notification text is worth showing; tool events pass
        # None so the daemon preserves the prompt across the whole turn
        if event == "UserPromptSubmit":
            params["message"] = squash(data.get("prompt") or "")
        elif event in ("Notification", "PermissionRequest"):
            params["message"] = squash(data.get("message") or "awaiting approval")
        elif event == "SessionStart":
            params["message"] = ""

        if not ensure_daemon(args.socket):
            return 0
        call(args.socket, "agent.report", params, timeout=1.0)
    except Exception:
        pass
    return 0


def cmd_send(args):
    pane = resolve_pane(args.socket, args.id)
    text = " ".join(args.text)
    tmux("send-keys", "-t", pane, "-l", "--", text, check=True)
    if not args.no_enter:
        time.sleep(0.2)  # let the TUI ingest the text before submit
        tmux("send-keys", "-t", pane, "Enter", check=True)


def cmd_peek(args):
    pane = resolve_pane(args.socket, args.id)
    out = tmux("capture-pane", "-p", "-t", pane, "-S", f"-{args.lines}", check=True)
    print(out.rstrip("\n"))


# --------------------------------------------------------------------------
# monitor
# --------------------------------------------------------------------------

COLORS = {"working": "\033[33m", "blocked": "\033[1;31m", "done": "\033[32m",
          "idle": "\033[2m", "unknown": "\033[2m", "gone": "\033[35m"}
RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"


def draw(agents, note):
    cols, lines = shutil.get_terminal_size()
    out = ["\033[2J\033[H"]
    out.append(f" {BOLD}herdlet{RESET} {DIM}· {len(agents)} agent(s) · "
               f"{time.strftime('%H:%M:%S')}{RESET}\n\n")
    if not agents:
        out.append(f"  {DIM}no agents registered yet{RESET}\n")
    id_w = max([len(r['id']) for r in agents] + [2])
    where_w = max([len(r['where']) for r in agents] + [2])
    for i, rec in enumerate(agents[:min(9, lines - 5)]):
        color = COLORS.get(rec["state"], "")
        line = (f"  {DIM}{i + 1}{RESET} {color}●{RESET} "
                f"{rec['id'].ljust(id_w)}  {color}{rec['state'].ljust(7)}{RESET}  "
                f"{rec['age'].rjust(3)}  {DIM}{rec['where'].ljust(where_w)}{RESET}  "
                f"{rec.get('message') or ''}")
        out.append(line[:cols + len(line) - len_visible(line)] + RESET + "\n")
    out.append(f"\n {DIM}q quit · 1-9 jump to pane{RESET}")
    if note:
        out.append(f"  {COLORS['blocked']}{note}{RESET}")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def len_visible(text):
    return len(re.sub(r"\033\[[0-9;]*m", "", text))


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
            if not _monitor_session(args.socket, fd):
                return 0
            time.sleep(1)  # daemon dropped; retry
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()


def _monitor_session(sock_path, stdin_fd):
    """One connected stretch. Returns False to quit, True to reconnect."""
    import selectors

    try:
        conn = socket.socket(socket.AF_UNIX)
        conn.connect(sock_path)
        conn.setblocking(False)
        conn.send((json.dumps({"id": "m", "method": "subscribe", "params": {}}) + "\n").encode())
    except OSError:
        draw([], "daemon not running, retrying...")
        return True

    sel = selectors.DefaultSelector()
    sel.register(conn, selectors.EVENT_READ)
    sel.register(stdin_fd, selectors.EVENT_READ)
    agents = []

    def refresh():
        nonlocal agents
        try:
            resp = call(sock_path, "agent.list", {}, timeout=2.0)
            agents = annotate(resp.get("result", {}).get("agents", []), pane_map())
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
                    if ch in ("q", "\x03"):
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


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

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
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser("get", help="get one agent's record")
    p.add_argument("--id", help="agent id (default: $HERDLET_ID or $TMUX_PANE)")
    p.set_defaults(fn=cmd_get)

    p = sub.add_parser("list", help="list registered agents")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("remove", help="remove an agent from the registry")
    p.add_argument("--id", help="agent id (default: $HERDLET_ID or $TMUX_PANE)")
    p.set_defaults(fn=cmd_remove)

    p = sub.add_parser("wait", help="block until an agent reaches a state")
    p.add_argument("--id", required=True)
    p.add_argument("--state", required=True, help="target state(s), comma-separated")
    p.add_argument("--timeout", type=float, help="seconds (exit 2 on timeout)")
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
    p.set_defaults(fn=cmd_peek)

    p = sub.add_parser("monitor", help="live status view (made for a tmux popup)")
    p.set_defaults(fn=cmd_monitor)

    args = parser.parse_args()
    try:
        sys.exit(args.fn(args) or 0)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
