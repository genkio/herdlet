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

There is no daemon to babysit: `hook`, `report`, `spawn` and `monitor`
auto-start it on first use (`herdlet serve` runs it in the foreground if you
prefer). After upgrading, restart it so the 0.10.0 daemon-side fixes are
served: the single-daemon election on `<socket>.lock`, waits pinned to the
occupant they started on, per-server pane identity, and the earlier `pair` /
`unpair`, `peer_send`, `limited` sweep and merge-key features. Run
`pkill -f 'herdlet.*serve'` and then any herdlet command; agents re-register on
their next hook event. Every command except
`hook` warns on stderr when it finds a daemon older than itself.

## Quickstart

```bash
herdlet report --id builder --state working --message "npm test"
herdlet list
# ID       STATE    AGE  AGENT  MODEL  PANE  WHERE       MESSAGE
# builder  working  2s   -             %5    dots:1 zsh  npm test

herdlet wait --id builder --state done,blocked,limited --timeout 600  # push-woken, no polling
herdlet wait --id builder,tester --state done,blocked --timeout 600  # any-of: wakes on whichever first
herdlet wait --prefix myproject/ --state blocked --timeout 600       # anyone in the project stuck?
herdlet wait --id builder --state blocked --edge --timeout 600  # ignore stale state, wake on a fresh report only
herdlet wait --id builder --on-compact --timeout 600         # wake on the next context compaction
herdlet wait --id builder --state done --timeout 600 --timeout-ok  # timeout is a result, exit 0
herdlet watch                                    # stream every state change as JSON lines
herdlet list --here                              # scope to the current tmux session
herdlet list --prefix myproject/                 # scope to one project's agents

herdlet wait --id builder --match 'tests? passed|ERROR' --timeout 600  # wait on pane OUTPUT (plain commands too)

herdlet spawn --id myproject/dev --model sonnet --effort medium --brief plans/dev.md  # new pane, registered from t=0
herdlet spawn --agent codex --id myproject/review --model gpt-5.6-sol --effort low --sandbox read-only --allow "git status"
herdlet send --id builder "run the tests again"  # types into builder's pane + Enter
herdlet send --id builder --file plans/next.md   # long or multi-line message from a file ('-' = stdin)
herdlet peek --id builder --lines 40             # read builder's recent output (--join unwraps soft wraps)
herdlet peek --id builder --transcript --lines 2 # read builder's own transcript instead of the pane
herdlet approve --id builder                     # choose the first one-time Yes, then echo the pane
herdlet approve --id builder --choice always     # choose the matching don't-ask-again option
herdlet approve --id builder --choice no --wait  # deny, then plain-wait for the next real state
herdlet pair --id dev --with tester --topic plans/repro.md  # scoped peer channel between two workers
herdlet ack --id builder                         # collected the result: done -> idle (list = inbox)
herdlet ack --id builder --kill-pane             # also close a finished worker pane
herdlet remove --id builder --kill-pane          # remove the record and safely close its pane
herdlet resume --id builder                      # agent died? type its native resume command into the pane
herdlet monitor                                  # live TUI (made for a tmux popup)
```

### Exit codes

| Code | Meaning |
|---:|---|
| 0 | Success |
| 1 | General command or daemon error |
| 2 | Wait timeout |
| 3 | Peer-scope refusal |
| 4 | Send refusal or submission error |
| 5 | Approve menu or choice not found |
| 6 | Send input box not found |

`wait` exits 2 on timeout, which is what the chunked-wait loop below keys on.
Harnesses that surface a background command's exit code as a failure (Claude
Code's background Bash among them) should pass `--timeout-ok`: the timeout comes
back as `result.type: "timeout"` with exit 0. `approve --wait --timeout-ok`
takes the flag too, where it only changes the exit code (0 instead of 2);
`approve` prints a state line, not JSON. Neither form hides a hung daemon,
which still fails.

With `--wait`, approve changes the matching `blocked` record to `working`.
Then a plain wait returns the next real state, including one that arrived during
the settle period.

`approve` selects `--choice yes` by default. `--choice always` selects a visible
"do not ask again" or "always" option. If that option is absent, it selects
the one-time Yes option and writes a note. On a Codex trust menu, `yes` and
`always` both select option 1.

Use `--option N` only as a raw escape hatch. Codex menu lengths vary, so a
digit can mean Yes on one menu and No on another. For Codex, `always` suppresses
only the exact command prefix that the menu shows.

`approve` types only when the visible pane contains a supported permission,
approval, or trust menu. Without a menu, it exits 5 and shows the last five
non-empty lines on stderr. It also changes a stale `blocked` record to `working`
because another user already answered the menu.

`ack --kill-pane` and `remove --kill-pane` close panes for finished records or
stale shell panes. They do not close a fresh worker behind a shell wrapper.
Pass `--force` to override this guard.

Agent ids resolve from `--id`, then `$HERDLET_ID`, then `$TMUX_PANE`. Name an
agent by launching it with an env var: `HERDLET_ID=builder claude`.

`send` serializes messages for each target pane. It waits up to five seconds for
existing input to clear before it types. If the input stays, it exits 4 and types
nothing. If no input box is visible, it exits 6 and shows the pane tail - except
in a pane sitting at a bare shell, where there is nothing to detect: there the
text is typed unverified (a note goes to stderr), so `send` can drive plain
shells and one-shot commands without `--no-verify`.

After Enter, `send` makes sure that the input box is empty. It sends Enter one
more time if the text remains. If the second attempt fails, `send` exits 4 and
leaves the text in the prompt. Use `--settle SECONDS` to change both wait times.

Pass `--ack` to wait for the target hook to record the prompt and the `working`
state. An unregistered pane has no hooks, so `send` skips this wait and writes a
note. Pass `--json` to print the send result.

Pass `--no-verify` to bypass both input checks and use fire-and-forget behavior.
The `--no-enter` flag implies `--no-verify` and types without submission.

Short text uses `tmux send-keys`. Text that is multi-line or more than 200
characters uses one bracketed paste. Thus, the receiving TUI cannot submit half
of the text. A plain shell uses canonical tty mode and drops an input line over
1023 characters. Agent TUIs use raw mode and do not have this limit.

## Automatic state from Claude Code / Codex hooks

`herdlet hook` reads the hook JSON on stdin, maps events to states, and
reports on behalf of the agent sitting in the pane. It auto-starts the daemon,
never blocks, and always exits 0, so it is safe in any hook chain.

| hook event | state |
|---|---|
| SessionStart | idle |
| UserPromptSubmit, PreToolUse, PostToolUse | working |
| Notification (permission), PermissionRequest | blocked |
| PreCompact | state and message unchanged, `compacts` +1 (see `herdlet get`) |
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

Hooks also record each agent's transcript path, which is what
`herdlet peek --id <agent> --transcript` reads: the last N assistant messages
straight out of the agent's own jsonl, text blocks only, instead of whatever
happens to be on screen. It is exact where a pane capture is lossy (wrapping,
a full-screen TUI, a scrolled-off answer). Agents without a transcript path
(codex, opencode, records from before 0.7) keep plain `peek`.

`list` and `monitor` cross-check the registry against reality: an agent
whose pane is gone shows `gone`; one whose pane fell back to a bare shell
*and whose record has gone quiet* shows `stale` (the process died without a
hook firing - resume it). A live worker whose pane merely shows a shell (a
wrapper script, `-p` piped to `tee`, a shell tool call) is not flagged, because
its hooks keep the record fresh.

## The `limited` state

An agent parked on its harness's usage-limit banner fires no hook, so its
record would say `working` forever and every waiter on it would run to timeout.
The daemon closes that hole: every `HERDLET_LIMIT_INTERVAL` seconds (default 30)
it reads the last 8 non-blank lines of the visible pane of each `working` / `spawning`
agent and, on a match against `HERDLET_LIMIT_PATTERN`, reports state `limited`
with message `usage limit banner in pane`. That is a normal report, so waiters
wake at once. (`blocked` is not swept: it is already a wake signal.)

- the default pattern matches Claude Code's own banner wording (`Usage limit
  reached ...`, `You've hit your session limit ...`, `You're out of usage
  credits`, `Your org is out of usage ...`). Override it for another harness.
- deliberately excluded: `Approaching your 5-hour usage limit ...`, which is a
  warning while the agent keeps working, and the fast-mode limits
  (`You've hit your fast limit`, `Fast limit reached and temporarily
  disabled`), where fast mode simply falls back to the normal one.
- only the bottom of the *visible* pane counts, because that is where Claude
  Code draws the banner. An old banner scrolled up into history does not count,
  and a record is only flipped if it has also been quiet for a full sweep
  interval, so a worker that auto-resumed is not dragged back to `limited`.
- `limited` is live, not terminal: the record is never pruned as finished, and
  the next real hook event overwrites it. It can still go `stale` if its pane
  falls back to a shell, which means the process died and needs `resume`.
- put it in your waits: `--state done,blocked,limited`.
- `HERDLET_LIMIT_SWEEP=0` turns the sweep off. Detection is a pane read, so a
  worker running a full-screen TUI on the alternate screen is invisible to it
  (see the renderer note below), and the daemon only sees panes on the tmux
  server it inherited `$TMUX` from.
- residual false positive: a pane that prints the banner wording itself (you
  `peek` a limited worker into your own pane) is flagged until its next hook.

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
    "PreCompact":       [{ "hooks": [{ "type": "command", "command": "command -v herdlet >/dev/null && herdlet hook || true" }] }],
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
herdlet spawn --id personal/herdlet/dev    --model sonnet --effort medium --brief plans/dev.md
herdlet spawn --id personal/herdlet/tester --model haiku  --effort low    --brief plans/tester.md
herdlet spawn --agent codex --id personal/herdlet/review --model gpt-5.6-sol --effort low --sandbox read-only
```

`spawn` launches Claude Code by default. Pass `--agent codex` for Codex. Its
default sandbox is `workspace-write`, and its default approval policy is
`on-request`. Repeat `--allow "<command prefix>"` to add worker commands to the
allowlist in the worker cwd. `spawn` keeps the caller in the left half. It
stacks workers at equal heights in the right half. A window under 160 columns,
or a stack below `--min-height 12`, puts the new worker in a new window.
`--vertical` keeps the old explicit split behavior. The command registers the
worker as `spawning` and links itself to the worker one way (see "Peer channel").
It waits for a Claude hook or the Codex input
prompt, then hands the worker its brief. `--model` and
`--effort` are required on purpose: a worker is a
top-level session, so anything you do not pin explicitly runs on your MAIN
(priciest) model, and that is the whole cost lever. Other agents (opencode or a
wrapper that sets a custom endpoint) still launch by hand with
`tmux split-window`; see the skill for that recipe.

Spawn JSON reports `placement` as `right-stack`, `new-window`, or `vertical`.

For Codex, `--sandbox danger-full-access --approval never` is the equivalent
of Claude's `bypassPermissions`. It gives the worker no prompts and no sandbox.
Git is the only guard. Use this combination only in a disposable worktree or a
repository you can reset.

then drives the pair with `send` / `wait --state done,blocked,limited` / `peek`,
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
herdlet spawn --id proj/worker --model haiku --effort low --brief plans/worker.md
herdlet spawn --agent codex --id proj/reviewer --model gpt-5.6-sol --effort low --sandbox read-only --brief plans/review.md
herdlet wait --id proj/worker --state done,blocked,limited --timeout 900
herdlet peek --id proj/worker --transcript --lines 2
herdlet send --id proj/worker "now fix the failing test"
```

The waiter is woken by a push from the daemon, not a polling loop.

For an agent other than Claude Code or Codex, or a one-shot `-p` wrapper, build
the pane yourself and let the worker's hooks register it:

```bash
tmux split-window -d -P -F '#{pane_id}' -t "$TMUX_PANE" "HERDLET_ID=proj/worker $LAUNCH -n 'proj/worker: test suite' --model <cheap-id> -p 'run the test suite'"
```

If a worker runs a full-screen TUI (Claude Code's `tui: fullscreen`), its
transcript lives in the terminal's alternate screen buffer, which `peek` /
`wait --match` (via `tmux capture-pane`) can't read - launch such workers in
the classic renderer (`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1`) so their panes
stay scrapeable. State detection is hook-driven and works regardless.

The skill bakes in the economics lessons of running herds for real: pick a
model per role by tier, never by inheriting the default (a worker launched
bare takes your priciest MAIN model - a herd of those burns tokens fast),
provision worker permissions at spawn time instead of babysitting menus, wait
on the whole herd in one long call, and prefer short-lived phase-scoped workers
over one pane dragging a huge context through an entire project.

## Peer channel

By default the master is the only hub: every worker-to-worker exchange goes
through it and costs the expensive thread a turn. Two loops don't need it:

- **implementer <-> tester** - repro steps, "fixed, retest", which log line to
  look at
- **reviewer <-> implementer** - minor findings, "that's intentional, here's
  why"

`pair` gives those two a direct channel, scoped so it can't grow into a second
management layer:

```bash
herdlet pair --id proj/dev --with proj/tester --topic plans/repro.md
herdlet unpair --id proj/dev --with proj/tester
```

The link is symmetric: both records gain `peers: ["<other>"]` and a `topics`
entry (`herdlet get`, `list --json`, and a `PEERS` column in `list` when anyone
has one). An agent may have several peers. Both ids must already be registered.

The scope rule: a worker's own `herdlet send` (identified by `$HERDLET_ID`) may
only reach a peer it was paired with. Anything else is refused with exit 3 and
`not paired with <target>; raise it in your report to the master`, so scope,
interface and money decisions stay on the one-way path up. The master itself has
no `$HERDLET_ID` and can still send anywhere.

It is a guardrail, not a sandbox: `HERDLET_ID= herdlet send ...`, `approve` and
`resume` all reach any pane, and an agent that shells out to `tmux send-keys`
was never going through herdlet in the first place. Its job is to keep the
default path honest, so a worker escalates by habit instead of quietly building
a second management layer.

`spawn` links the spawner to the worker it just created, so a nested master
(which `spawn` gives a `HERDLET_ID`, making it a worker to the rule above) can
still drive its own children. That link is **one-directional**: only the
spawner's record gains the child, so the child has no shortcut back into its
master's pane - it reports upward like any other worker. The topic is the
`--brief` path, or `<cwd>/plans/<id with / as ->-thread.md` when there is no
brief. Only an explicit `herdlet pair` is symmetric; `unpair`, `remove` and the
prune sweep clean both sides either way. In short: you may talk to what you
spawned, and to whoever the master paired you with.

Every peer send appends one line to the topic file, under a `## Thread` heading
created on first use:

```
## Thread

- 2026-09-09T14:02:11+0900 proj/dev -> proj/tester: fixed in abc123, please retest
```

The line is the first 120 characters, newlines collapsed - the full text went
to the pane, and the file is an audit trail, not a message queue. The daemon
emits a `peer_send` event (`from`, `to`, `topic`, `chars`) on `herdlet watch`
and touches no record, so a wait on the master's OWN state is never woken. The
receiving peer still transitions through its own hooks, so a master waiting on
that peer's `done` does wake, exactly as before. Peers survive `resume` and an
`ack` that flips `done` -> `idle`; acking an `ended` agent removes its record,
and `remove` drops the id from its peers' lists.

Pattern for the master to paste into a worker's brief:

> You are paired with `<id>` on `<topic>`. Send it repros and answers directly
> with `herdlet send --id <id> ...`. Do not copy the master. Decisions about
> scope, interface or money go in your report, not to your peer.

## Protocol

Newline-delimited JSON over `~/.herdlet.sock` (override with `--socket` or
`$HERDLET_SOCKET`). Requests: `{"id", "method", "params"}`; responses:
`{"id", "result"}` or `{"id", "error"}`.

Methods: `ping`, `agent.report`, `agent.get`, `agent.list`, `agent.remove`,
`agent.pair` / `agent.unpair` (`{id, with, topic}`, symmetric unless
`agent.pair` gets `oneway: true`, which links `id -> with` only), `peer.send`
(`{from, to, topic, chars}`, fans out a `peer_send` event and nothing else),
`wait` (`{id | ids | prefix, states, timeout_ms, edge?}`, wakes on the first
matching agent; the result carries `matched`, every agent currently in a
target state, so a herd wait can batch-collect instead of re-waiting per
straggler), `subscribe` (`{id?, state?}`, connection then streams
`agent.state_changed` / `agent.removed` events).

A `PreCompact` hook emits a `compacted` watch event. Pass `wait --on-compact`
to wake on the next counter increase. The `list` STATE column adds `C<n>` when
the counter is more than zero.

A `blocked` agent is re-announced to waiters every `HERDLET_BLOCKED_REEMIT`
seconds (default 30, 0 disables), so a `wait` - especially `--edge` - that
started *after* the agent was already blocked still wakes instead of starving.

Report merge semantics: absent/null fields preserve the previous value, empty
string clears (merge keys: `message`, `agent`, `pane`, `cwd`, `session`,
`transcript`, `model`, `effort`, `tmux`). Tool-use hooks report `message: null`,
which is why the prompt survives as the message for the whole turn. `compacts`
is a daemon-side counter, bumped by `{"compact": true}` reports. `peers` /
`topics` are owned by `agent.pair`, never by a report, and default to `[]` /
`{}` on a record written by an older daemon.

`tmux` is the tmux server socket the pane lives on (from `$TMUX`). Pane ids
repeat across tmux servers, so every pane operation on a record uses that
record's server: the `limited` sweep, `list` / `monitor` annotation, `peek`,
`wait --match`, `send`, `approve`, `resume` and `--kill-pane`. A daemon started
from one server no longer reads or types into the same-numbered pane of another
one, and `list` from a different server no longer shows those agents as `gone`.
Records written before this key existed fall back to the local server, the only
one a pre-fix daemon could have reported from. `wait` also pins the occupant
instance it started on, so a re-registered id (same name, new pane) cannot
satisfy an older waiter.

Environment: `HERDLET_SOCKET`, `HERDLET_ID`, `HERDLET_SKIP`,
`HERDLET_BLOCKED_REEMIT`, `HERDLET_MAX_AGE`, `HERDLET_PRUNE_INTERVAL`,
`HERDLET_LIMIT_SWEEP`, `HERDLET_LIMIT_INTERVAL`, `HERDLET_LIMIT_PATTERN`.

## Development

```bash
make test
```

## License

MIT
