# herdlet

Tiny coordination bus for coding agents (Claude Code, Codex, ...) running in
tmux panes.

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

Single file, stdlib only, no dependencies beyond python3 and tmux.

## Install

```bash
brew install genkio/tap/herdlet
```

Or just drop `herdlet.py` somewhere on your PATH.

There is no daemon to babysit: `hook`, `report` and `monitor` auto-start it
on first use (`herdlet serve` runs it in the foreground if you prefer).

## Quickstart

```bash
herdlet report --id builder --state working --message "npm test"
herdlet list
# ID       STATE    AGE  AGENT  PANE  WHERE       MESSAGE
# builder  working  2s   -      %5    dots:1 zsh  npm test

herdlet wait --id builder --state done,blocked --timeout 600   # push-woken, no polling
herdlet watch                                    # stream every state change as JSON lines

herdlet send --id builder "run the tests again"  # types into builder's pane + Enter
herdlet peek --id builder --lines 40             # read builder's recent output
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
| SessionEnd | removed from registry |

The prompt text becomes the agent's `message`, so `list` / `monitor` show
what each agent is working on.

Claude Code `settings.json` (same pattern for Codex `hooks.json`, with
`--agent codex`):

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

## Agent-to-agent orchestration

Give your agents the included [SKILL.md](SKILL.md) (drop it in
`~/.claude/skills/herdlet/`) and they can coordinate themselves:

```bash
# spawn a worker in a new pane, wait for it, read its result
tmux split-window -d -P -F '#{pane_id}' "HERDLET_ID=worker claude -p 'run the test suite'"
herdlet wait --id worker --state done,blocked --timeout 900
herdlet peek --id worker --lines 40
herdlet send --id worker "now fix the failing test"
```

The waiter is woken by a push from the daemon, not a polling loop.

## Protocol

Newline-delimited JSON over `~/.herdlet.sock` (override with `--socket` or
`$HERDLET_SOCKET`). Requests: `{"id", "method", "params"}`; responses:
`{"id", "result"}` or `{"id", "error"}`.

Methods: `ping`, `agent.report`, `agent.get`, `agent.list`, `agent.remove`,
`wait` (`{id, states, timeout_ms}`), `subscribe` (`{id?, state?}`, connection
then streams `agent.state_changed` / `agent.removed` events).

Report merge semantics: absent/null fields preserve the previous value, empty
string clears. Tool-use hooks report `message: null`, which is why the prompt
survives as the message for the whole turn.

## Development

```bash
make test
```

## License

MIT
