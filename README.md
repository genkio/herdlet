# herdlet

Tiny coordination bus for coding agents (Claude Code, Codex, opencode, ...)
running in tmux panes.

tmux already gives you the multiplexing and the pane I/O (`send-keys`,
`capture-pane`). What it doesn't have is the layer that makes multi-agent
work pleasant:

- **semantic agent state** - who is `working`, `blocked` on approval, `done`, `idle`
- **push events** - subscribe / wait instead of capture-pane polling loops
- **a registry** - agents address each other by name, not by guessing pane ids

herdlet is that missing layer: one small daemon on a unix socket, speaking
newline-delimited JSON. The protocol deliberately mimics the coordination
subset of [herdr](https://github.com/ogulcancelik/herdr)'s socket API
(`agent.report` ≈ `pane.report_agent`, `subscribe` ≈ `events.subscribe`,
`wait` ≈ `herdr wait agent-status`). If you want a full agent-native
multiplexer, use herdr. If you want to keep your tmux setup and just add the
coordination layer, use herdlet.

Last herdr idea-scan: **v0.7.3** (commit `3b8aeee`, 2026-07). Borrowed as of
herdlet 0.4.x: session-ref capture for `resume`, process-liveness `stale`
detection, the seen-bit (`ack`), output-match waits, bracketed-paste `send`,
registry persistence. Deliberately skipped: screen-scraping detection
manifests (high maintenance; hooks suffice) and live server handoff (tmux
owns the PTYs, so the problem doesn't exist here). To mine future herdr
releases, diff its CHANGELOG from v0.7.3 forward.

Single file, stdlib only, no dependencies beyond python3 and tmux.

## Install

```bash
brew install genkio/tap/herdlet
herdlet setup                # wire hooks + skill + permissions, one time
```

Or just drop `herdlet.py` somewhere on your PATH.

`herdlet setup` wires the Claude Code / Codex hooks (backing up the settings
files it touches), installs the agent skill, and allowlists `Bash(herdlet:*)`.
Add `--allow-tmux` if agents should also spawn panes unprompted. It is
idempotent and leaves everything else in your settings alone. Prefer manual
wiring? The snippets are below.

There is no daemon to babysit: `hook`, `report` and `monitor` auto-start it
on first use (`herdlet serve` runs it in the foreground if you prefer).
After upgrading, restart it so new protocol features (any-of `wait`, the
`blocked` re-announce, the `matched` batch) are served:
`pkill -f 'herdlet.*serve'`; agents re-register on their next hook event.

## Quickstart

```bash
herdlet report --id builder --state working --message "npm test"
herdlet list
# ID       STATE    AGE  AGENT  PANE  WHERE       MESSAGE
# builder  working  2s   -      %5    dots:1 zsh  npm test

herdlet wait --id builder --state done,blocked --timeout 600   # push-woken, no polling
herdlet wait --id builder,tester --state done,blocked --timeout 600  # any-of: wakes on whichever first
herdlet wait --prefix myproject/ --state blocked --timeout 600       # anyone in the project stuck?
herdlet wait --id builder --state blocked --edge --timeout 600  # ignore stale state, wake on a fresh report only
herdlet watch                                    # stream every state change as JSON lines
herdlet list --here                              # scope to the current tmux session
herdlet list --prefix myproject/                 # scope to one project's agents

herdlet wait --id builder --match 'tests? passed|ERROR' --timeout 600  # wait on pane OUTPUT (plain commands too)

herdlet send --id builder "run the tests again"  # types into builder's pane + Enter (multi-line = one bracketed paste)
herdlet peek --id builder --lines 40             # read builder's recent output (--join unwraps soft wraps)
herdlet approve --id builder                     # answer a permission menu (option 1), echo the pane
herdlet approve --id builder --wait              # answer, mark working, edge-wait for the next transition, show the pane
herdlet ack --id builder                         # collected the result: done -> idle (list = inbox)
herdlet resume --id builder                      # agent died? type its native resume command into the pane
herdlet monitor                                  # live TUI (made for a tmux popup)
```

Agent ids resolve from `--id`, then `$HERDLET_ID`, then `$TMUX_PANE`. Name an
agent by launching it with an env var: `HERDLET_ID=builder claude`.

## Automatic state from Claude Code / Codex hooks

`herdlet hook` reads the hook JSON on stdin, maps events to states, and
reports on behalf of the agent sitting in the pane. It auto-starts the daemon,
never blocks, and always exits 0, so it is safe in any hook chain.

| hook event | state |
|---|---|
| SessionStart | idle |
| UserPromptSubmit, PreToolUse, PostToolUse | working |
| Notification (permission), PermissionRequest | blocked |
| Stop | done |
| SessionEnd | ended (record kept, with its session ref) |

The prompt text becomes the agent's `message`, so `list` / `monitor` show
what each agent is working on. Hooks also record the agent's native session
id, which is what powers `herdlet resume` (types `claude --resume <id>` /
`codex resume <id>` / `opencode --session <id>` into the pane after a crash or
usage-limit kill). A finished session becomes `ended` rather than vanishing, so
you can still collect its output and resume it; `herdlet remove` (or `ack`)
clears it. The registry self-cleans: terminal records are dropped 24h after
finishing, and ANY record untouched for `HERDLET_MAX_AGE` (default 3d, 0
disables) is dropped whatever its state - the days-dead panes a terminal-only
TTL never catches. A still-live agent just re-registers on its next hook; the
daemon sweeps hourly and also on load.

opencode has no shell-hook config, so `herdlet setup` installs a small plugin
(`~/.config/opencode/plugins/herdlet.js`) that reports the same states from
opencode's event stream.

`list` and `monitor` cross-check the registry against reality: an agent
whose pane is gone shows `gone`; one whose pane fell back to a bare shell
*and whose record has gone quiet* shows `stale` (the process died without a
hook firing - resume it). A live worker whose pane merely shows a shell (a
wrapper script, `-p` piped to `tee`, a shell tool call) is not flagged, because
its hooks keep the record fresh.

`herdlet setup` wires all of this for you; the snippets below are the manual
reference. Claude Code `settings.json` (same pattern for Codex `hooks.json`,
with `--agent codex`):

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "command -v herdlet >/dev/null && herdlet hook || true" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "command -v herdlet >/dev/null && herdlet hook || true" }] }],
    "PostToolUse":      [{ "hooks": [{ "type": "command", "command": "command -v herdlet >/dev/null && herdlet hook || true" }] }],
    "Notification":     [{ "matcher": "permission_prompt|elicitation_dialog",
                           "hooks": [{ "type": "command", "command": "command -v herdlet >/dev/null && herdlet hook || true" }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "command -v herdlet >/dev/null && herdlet hook || true" }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "command -v herdlet >/dev/null && herdlet hook || true" }] }]
  }
}
```

## The monitor

`herdlet monitor` is a live who-is-stuck view: agents sorted blocked-first,
color-coded, with age and message. Press `1`-`9` to jump straight to that
agent's pane, `q` to quit. Wire it to a tmux popup:

```tmux
bind m display-popup -E -w 80% -h 60% -T " agents " "herdlet monitor"
```

## Layout: sessions are domains, windows are projects, panes are roles

herdlet's namespace is global (one bus per machine), so structure comes from
two conventions, not infrastructure:

```
session "work"                      session "personal"
├── window 0: master  <- you        ├── window 0: master  <- you
├── window 1: billing-api           ├── window 1: herdlet
│   ├── work/billing/planner       │   ├── personal/herdlet/dev
│   ├── work/billing/dev           │   └── personal/herdlet/tester
│   └── work/billing/tester        └── window 2: genkia
└── window 2: admin-ui                  └── personal/genkia/dev
```

- **One tmux session per domain** (work, personal, ...). Each domain gets a
  long-lived **master**: an interactive agent in window 0 that you talk to.
- **One window per project**, **one pane per role**, spawned by the master on
  demand.
- **Name agents `project/role`** via `HERDLET_ID`. Names are the only thing
  that can collide across projects; the prefix makes them unique, and
  `herdlet list --prefix herdlet/` or `--here` keeps discovery scoped.
  Unnamed agents fall back to their pane id, which never collides.

A master's turn looks like: you say "let's work on herdlet: spin up a dev and
a tester, requirement is ...", and it runs

```bash
tmux new-window -t personal -n herdlet -c ~/code/herdlet
tmux split-window -h -t personal:herdlet
tmux send-keys -t personal:herdlet.0 "HERDLET_ID=personal/herdlet/dev $LAUNCH --model <mid-id>" Enter
tmux send-keys -t personal:herdlet.1 "HERDLET_ID=personal/herdlet/tester $LAUNCH --model <cheap-id>" Enter
```

(`$LAUNCH` = however you launch your agent - `claude`, `codex`, or a wrapper
that sets a custom endpoint/model; swap `<mid-id>`/`<cheap-id>` for model ids
valid in your setup. a worker is a top-level session, so it takes your MAIN
model unless you downgrade it explicitly - that is the cost lever.)

then drives the pair with `send` / `wait --state done,blocked` / `peek`,
relaying between roles and reporting back to you. Hours later, "now genkia"
just means a new window; the herdlet window keeps existing and its agents show
`idle` in the monitor. Two masters never interfere: each spawns only into its
own session and its own id prefixes. Scope each domain's popup with
`herdlet monitor --session work`.

Masters shell out to `tmux` and `herdlet` constantly, so either run
`herdlet setup --allow-tmux` or expect to approve every step by hand.

## Agent-to-agent orchestration

Give your agents the included [skill](skills/herdlet/SKILL.md) and they can
coordinate themselves:

```bash
npx skills add genkio/herdlet        # Claude Code, Codex, Cursor, ...
# or manually: cp skills/herdlet/SKILL.md ~/.claude/skills/herdlet/
```

```bash
# spawn a worker in a new pane, wait for it, read its result
tmux split-window -d -P -F '#{pane_id}' "HERDLET_ID=worker $LAUNCH --model <cheap-id> -p 'run the test suite'"
herdlet wait --id worker --state done,blocked --timeout 900
herdlet peek --id worker --lines 40
herdlet send --id worker "now fix the failing test"
```

The waiter is woken by a push from the daemon, not a polling loop.

The skill bakes in the economics lessons of running herds for real: pick a
model per role by tier, never by inheriting the default (a worker launched
bare takes your priciest MAIN model - a herd of those burns tokens fast),
provision worker permissions at spawn time instead of babysitting menus, wait
on the whole herd in one long call, and prefer short-lived phase-scoped workers
over one pane dragging a huge context through an entire project.

## Protocol

Newline-delimited JSON over `~/.herdlet.sock` (override with `--socket` or
`$HERDLET_SOCKET`). Requests: `{"id", "method", "params"}`; responses:
`{"id", "result"}` or `{"id", "error"}`.

Methods: `ping`, `agent.report`, `agent.get`, `agent.list`, `agent.remove`,
`wait` (`{id | ids | prefix, states, timeout_ms, edge?}`, wakes on the first
matching agent; the result carries `matched`, every agent currently in a
target state, so a herd wait can batch-collect instead of re-waiting per
straggler), `subscribe` (`{id?, state?}`, connection then streams
`agent.state_changed` / `agent.removed` events).

A `blocked` agent is re-announced to waiters every `HERDLET_BLOCKED_REEMIT`
seconds (default 30, 0 disables), so a `wait` - especially `--edge` - that
started *after* the agent was already blocked still wakes instead of starving.

Report merge semantics: absent/null fields preserve the previous value, empty
string clears (merge keys: `message`, `agent`, `pane`, `cwd`, `session`).
Tool-use hooks report `message: null`, which is why the prompt survives as
the message for the whole turn.

## Development

```bash
make test
```

## License

MIT
