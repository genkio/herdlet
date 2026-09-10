---
name: herdlet
description: "Coordinate with other coding agents running in tmux panes. See who is working/blocked/done, wait for another agent to finish, read a neighbor's output, send it instructions, and spawn worker agents in new panes. Use when running inside tmux and the herdlet CLI is available."
---

# herdlet - agent skill

before using this skill, check that you are inside tmux (`$TMUX_PANE` is set)
and `herdlet` is on PATH. if either is missing, say so and stop.

you are one of possibly several coding agents, each in its own tmux pane.
herdlet is a small coordination bus: every agent has an id, a semantic state,
and a registered pane. tmux does the terminal work; herdlet tells you who is
doing what, and lets you wait on other agents instead of polling their panes.

this means you can:

- see every registered agent, its state, and what it is working on
- block until another agent is `done` (push-woken, no polling loop)
- read a neighbor agent's recent terminal output
- type instructions into a neighbor agent's prompt
- spawn worker agents in new panes and collect their results

## concepts

**states**: `idle`, `working`, `spawning`, `blocked`, `limited`, `done`,
`ended` (custom strings allowed). `blocked` means the agent is waiting for a
human approval; `limited` means the daemon spotted a usage-limit banner in its
pane, so it is parked until the limit resets; `spawning` means `herdlet spawn`
registered it but its first hook has not fired yet; `done`
means its turn finished; `ended` means the whole session exited - the record is
KEPT (with its session ref) so you can still see it and `resume` it. states
update automatically via your harness's hooks (`herdlet setup` wires Claude
Code, Codex, and an opencode plugin), so you normally never report your own
state; an agent with no integration is tracked manually (`herdlet report`) or by
its output (`herdlet wait --match`).

**ids**: an agent's id is `$HERDLET_ID` if it was launched with one,
otherwise its tmux pane id like `%5`. ids come from `herdlet list`; do not
guess them. when you spawn a worker, name it via the env var so you can
address it. the namespace is machine-global, so name workers `project/role`
(e.g. `herdlet/tester`); a bare name like `dev` collides the moment a second
project uses it, and the newer registration silently steals the id.

**your own id**: `$HERDLET_ID` if set, else `$TMUX_PANE`.

**layout convention**: one tmux session per domain (work, personal), one
window per project, one pane per role. stay inside your own session and your
own id prefix unless explicitly asked to reach further.

## discover the herd

```bash
herdlet list                    # everyone, everywhere on this machine
herdlet list --here             # only agents in your tmux session (prefer this)
herdlet list --prefix herdlet/  # only one project's agents
herdlet list --json             # machine-readable
herdlet get --id herdlet/tester
```

`gone` means the agent's pane no longer exists. `stale` means the pane is back
at a bare shell AND the record has gone quiet - the agent process died (crash,
usage limit, ctrl-c) without a hook firing. a live worker whose pane happens to
show a shell (a wrapper script, `-p` piped to `tee`, a shell tool call) is NOT
flagged stale: its hooks keep the record fresh, and only a record that stops
updating trips the check. `ended` is a clean session exit. all three keep the
record, so see "resume a dead worker".

`limited` means the daemon read a usage-limit banner at the bottom of that
agent's pane. no hook fires when a worker hits its limit, so without this the
record would sit on `working` and every waiter would run to timeout. the worker
is alive and its context is intact: do NOT respawn it. wait for the reset, then
`send` it a short nudge to continue; if its process did die, `list` shows
`stale` instead and you `resume`. the next real hook event clears `limited` on
its own. it is a screen read, so it can be wrong: a pane that PRINTS that
wording (you `peek`ed a limited worker into your own pane) is flagged too. when
`limited` surprises you, `peek` before acting on it.

## wait for another agent

```bash
herdlet wait --id builder --state done --timeout 600
herdlet wait --id builder --on-compact --timeout 600
```

`--on-compact` wakes on the next increase of the target's `compacts` counter.
you can combine it with `--state` to wake for either event. a 120-second wait
used 33,648 KiB RSS with Python 3.14.6 on macOS.

always include `blocked` and `limited` in the states unless you specifically
want to sleep through them: a blocked agent will not finish until a human acts,
and a limited one will not finish until its usage limit resets.

```bash
herdlet wait --id builder --state done,blocked,limited --timeout 600
```

exit code 0 = state reached (`result.state` says which), 2 = timeout. always
pass `--timeout`. after waking, `peek` to see what actually happened.

if your harness reports a background command's non-zero exit as a FAILURE
(Claude Code's background Bash does), add `--timeout-ok`: the timeout comes back
as a normal result (`result.type` = `timeout`, exit 0) instead of an error, and
you decide whether to wait again. without the flag a plain timeout looks like a
crashed command. `approve --wait --timeout-ok` takes the flag too, where it only
changes the exit code (0 instead of 2); approve prints a state line, not JSON.
neither form hides a hung daemon, which still fails. leave the flag OFF in the
chunked loop below, which keys on exit 2.

to watch several agents at once, wait on all of them in one call; it wakes on
whichever transitions first (`result.id` says which). `result.matched` lists
EVERY agent already in a target state at wake time, so collect that whole
batch and only re-wait for the stragglers, instead of one wait per agent:

```bash
herdlet wait --id proj/dev,proj/tester --state done,blocked,limited --timeout 550
herdlet wait --prefix proj/ --state blocked,limited --timeout 550   # anyone stuck?
```

`--edge` ignores whatever state is already recorded and wakes only on a
fresh report. use it right after answering a menu: the registry still shows
the pre-answer `blocked` until the worker's next hook event, so a plain
`wait --state blocked` would match that stale state instantly instead of
waiting for the real next transition:

```bash
herdlet wait --id proj/dev --state done,blocked --edge --timeout 550
```

a `blocked` agent is re-announced to waiters periodically (every ~30s), so an
`--edge` wait you START while an agent is already stuck no longer starves - it
wakes on the next re-announce even without a fresh hook. you still won't get
INSTANT notice of an already-stuck agent under `--edge`; when you just want to
know who is stuck right now, use a plain `wait` or `list`, which return
immediately (`matched` carries the whole set).

to wait on terminal OUTPUT instead of agent state - a build finishing, a
server logging "listening", a test summary - match a regex against the
pane's recent lines. works on plain command panes too, which have no hook
state at all; never hand-roll sleep/curl polling loops:

```bash
herdlet wait --id builder --match 'listening on|ERROR' --timeout 550
```

existing content matches immediately, then it polls every 2s. exit 0 =
matched (`result.line` says what), 2 = timeout.

if your shell tool has its own timeout, size the wait just under the tool's
cap: every extra wake-up costs a full model turn. Claude Code's Bash defaults
to 2 minutes but takes a `timeout` parameter up to 600000 ms; pass that and
wait in ~550s chunks instead of many 90s ones. where the cap can't be raised,
loop chunks inside a single call:

```bash
while true; do
  herdlet wait --id herdlet/dev --state done,blocked --timeout 90 && break
  [ $? -eq 2 ] || break   # 2 = chunk timed out, keep waiting; anything else, stop
done
```

## read a neighbor's output

```bash
herdlet peek --id builder --lines 60
herdlet peek --id builder --transcript             # its last answer, verbatim
herdlet peek --id builder --transcript --lines 3   # its last 3 answers, oldest first
```

plain `peek` is that pane's visible scrollback tail, exactly what a human would
see. pass `--join` to unwrap soft-wrapped lines - better when grepping logs.

`--transcript` reads the worker's OWN transcript file instead of the screen and
prints the last N assistant messages (text blocks only; tool calls and thinking
are dropped). prefer it whenever you want what the worker SAID: a pane capture
is lossy - it wraps, it scrolls, and a full-screen TUI hides its history
entirely - while the transcript is the real text. it needs a `transcript` field
in the record, which Claude Code's hooks provide; for an agent without one it
says `no transcript recorded for <id>; use plain peek`. use plain `peek` for
what is on SCREEN right now: a permission menu, a banner, a running command.

## send instructions to another agent

```bash
herdlet send --id builder "run the full test suite and report failures"
herdlet send --id builder --file plans/round2.md     # long or multi-line message
git diff | herdlet send --id builder --file -        # or from stdin
```

the text is typed into that agent's terminal and submitted with Enter, as if
its human had typed it. sends to one pane are serialized. `send` waits up to
five seconds for existing input to clear before it types. if the input stays,
it exits 4 and types nothing. if no input box is visible, it exits 6 and shows
the pane tail.

if the target agent is mid-turn, the message queues as normal user input.

after Enter, `send` makes sure that the input box is empty. it sends Enter one
more time if the text remains. if the second attempt fails, it exits 4 and
leaves the text in the prompt. use `--settle SECONDS` to change both wait times.

pass `--ack` to wait for the target hook to record the prompt and the `working`
state. an unregistered pane has no hooks, so `send` skips this wait and writes a
note. pass `--json` to print the send result.

pass `--no-verify` to bypass both input checks and use fire-and-forget behavior.
`--no-enter` implies `--no-verify` and types without submission.

if YOU are a worker (`$HERDLET_ID` is set), `send` only reaches your peers - the
agents the master paired you with, plus any worker you spawned yourself.
anything else exits 3 with `not paired with <target>; raise it in your report to
the master`. that is the answer, not an obstacle - put it in your report. see
"talk to a peer directly".

anything multi-line or over 200 characters is delivered as one bracketed paste,
so embedded newlines read as text instead of submitting early and a long message
cannot be cut in half by the Enter that follows it. pass a long brief with
`--file` rather than as a shell argument: no quoting to get wrong, and no
argv-length ceiling. still prefer a brief FILE on disk plus a one-line
"read X and do it" for anything really big - it costs the worker one Read
instead of a wall of pasted text.

## talk to a peer directly

normally every worker-to-worker exchange goes through the master, and each hop
costs the most expensive agent in the herd a full turn. two loops don't need it:
implementer <-> tester (repro steps, "fixed, retest", which log line to read)
and reviewer <-> implementer (minor findings, "intentional, here is why"). a
master opens that channel:

```bash
herdlet pair --id proj/dev --with proj/tester --topic plans/repro.md
herdlet unpair --id proj/dev --with proj/tester
```

the link is symmetric - both records gain the other in `peers`, with the topic
file in `topics` (`herdlet get`, and a `PEERS` column in `list` once anyone has
one). both agents must already be registered. an agent can have several peers.
tell each of them so, in the brief:

> You are paired with `<id>` on `<topic>`. Send it repros and answers directly
> with `herdlet send --id <id> ...`. Do not copy the master. Decisions about
> scope, interface or money go in your report, not to your peer.

as a worker, `send` is scoped to your peers: a send to anyone else exits 3. that
is a guardrail to keep the default path honest, not an isolation boundary -
`HERDLET_ID= herdlet send`, `approve` and `resume` all still reach any pane, so
treat the refusal as the reminder it is rather than something to route around.
the split it enforces: mechanical back-and-forth goes sideways to your peer,
everything about scope, interfaces, money or risk goes UP in your report.

you may also talk to what you SPAWNED: `spawn` links you to each worker it
creates (topic = its `--brief`, else `<cwd>/plans/<id with / as ->-thread.md`),
so a nested master can drive its own children even though spawn gave it a
`HERDLET_ID`. that link is one-directional - only YOUR record gains the child.
a child cannot `send` up into its spawner: it reports upward like any other
worker, and only an explicit `herdlet pair` is symmetric.

a peer send touches no record, so a wait on the MASTER's own state is never
woken by it. the receiving peer still transitions through its own hooks, so a
master waiting on that peer's `done` wakes as usual.

each peer send appends one line to the topic file (`- <timestamp> <from> ->
<to>: <first 120 chars>`) under a `## Thread` heading. the full text went to
the pane, so the file is the audit trail the master reads later, not a
mailbox - never poll it for replies, and never treat it as a place
to hold a conversation. read your peer's actual answer with
`peek --transcript`, or just wait for it to `send` you one. the daemon also emits a `peer_send` event on `watch`.

## spawn a worker agent

for a Claude Code or Codex worker, use `herdlet spawn`. one command does the whole
launch: it builds the pane, pins the model and effort, mutes the human's
per-turn notifications, keeps the pane readable, registers the worker BEFORE its
first hook, waits for it to come up, and hands it its brief. Claude Code is the
default agent. pass `--agent codex` for Codex.

```bash
herdlet spawn --id proj/dev --model sonnet --effort medium --brief plans/dev.md
# spawned proj/dev in %7 (sonnet/medium)
herdlet spawn --agent codex --id proj/review --model gpt-5.6-sol --effort low --sandbox read-only --brief plans/review.md
# spawned proj/review in %8 (gpt-5.6-sol/low)
```

- `--id`, `--model` and `--effort` are required. never let a worker inherit the
  default model (see the tier table below); `--effort` is the same lever for
  output tokens, so keep mechanical roles on `low`.
- `--brief PATH` is the normal way to task a worker: the title defaults to the
  brief's first heading, and once the worker is up it is sent
  `Read <brief> and do it.` write the brief to a file first.
- `--title "<purpose>"` names a Claude Code session. for Codex, spawn stores the
  title in the registry message but does not pass it to the launch command.
- `--permission-mode <mode>` is for Claude Code only (default `auto`).
  `--sandbox <mode>` and `--approval <on-request|never>` are for Codex only.
  their defaults are `workspace-write` and `on-request`.
- repeat `--allow "<command prefix>"` to add commands to the worker's allowlist.
  Claude entries go in `.claude/settings.local.json`. Codex entries go in
  `.codex/rules/herdlet.rules`; Codex loads project rules after it trusts the cwd.
- other flags: `--cwd DIR`, `--vertical`, `--env K=V` (repeatable),
  `--ready-timeout SECONDS` (default 30), `--json`.
- exit 0 = the pane is up. either the worker registered and got its brief, or it
  had not reported within `--ready-timeout` but its pane is alive. in the second
  case **the brief was NOT sent** (the warning says so, and `--json` carries
  `brief_sent`): `peek` the pane, clear whatever it is sitting on, then `send`
  the brief yourself. Codex spawn accepts its preselected trust option once and
  continues to wait for the input prompt. pre-trusting the cwd avoids this prompt.
  exit 1 = the pane is already gone, so the launch command itself failed; check
  the model and effort you passed.
- spawn also links you to the worker (one way, downward), so you can `send` to
  it even when you are yourself a spawned agent (see "talk to a peer directly").
- the record exists from t=0 in state `spawning`, so the worker is addressable
  by NAME immediately, including for the trust prompt above. no pane-id-only
  window any more.
- if the window has no room to split, spawn opens a new window in your session
  instead and says so.

`spawn` supports Claude Code and Codex. for any other agent, build the pane by
hand as below.

spawn a non-claude worker with the **same launch command you were started
under**, not a bare vendor binary. that command carries your model routing,
endpoint/auth env, and per-role config; a bare `codex` in a fresh pane inherits
none of it and may hit the wrong endpoint or an unconfigured model. call it
`$LAUNCH` below and substitute your own:

| harness | `$LAUNCH` |
|---|---|
| Claude Code | `claude` |
| Claude Code via a custom endpoint (a wrapper you wrote that sets base url / key / model) | that wrapper |
| Codex | use `herdlet spawn --agent codex`; no hand-built pane |
| opencode | `opencode` (the `herdlet setup` plugin reports its state) |

a bare `$LAUNCH -p` pane CLOSES the moment the agent exits, destroying its
scrollback - by the time you peek, the result is gone. wrap a one-shot worker
in a shell that keeps the pane alive and emits its own done-marker AFTER the CLI
returns:

```bash
tmux split-window -d -P -F '#{pane_id}' -t "$TMUX_PANE" "bash -c '\
  HERDLET_ID=proj/worker $LAUNCH -n \"proj/worker: <purpose>\" --model <cheap-id> \
    --allowedTools Read Edit \"Bash(pnpm *)\" \
    -p \"read plans/worker.md and do it\" | tee /tmp/worker.out; \
  echo exit=\${PIPESTATUS[0]} > /tmp/worker.done; sleep 3600'"
herdlet wait --id proj/worker --state done,blocked --timeout 550
```

three rules baked into that wrapper, each a real failure it prevents:

- **the sentinel goes in the WRAPPER, never in the prompt.** telling the agent
  "end your output with WORKER_DONE" backfires: that text echoes into the pane
  the instant the prompt is submitted, so `wait --match WORKER_DONE` fires
  immediately on the echo, not on completion. wait on the hook-driven `done`
  state, or match a marker your shell writes after the CLI exits (the `.done`
  file above) - never one the agent is told to print.
- **`tee` eats the exit code.** `$?` after a pipe is `tee`'s (always 0); capture
  the agent's real exit with `${PIPESTATUS[0]}` (bash).
- **exit 0 is not "the job is done".** a `-p` worker can exit clean with a
  half-finished task (an internal tool error it "recovered" past, a truncated
  write). judge completion by the DELIVERABLE - `git status`, the file it was
  told to produce, its own final report - not by the exit code or a done-marker
  alone.

interactive workers don't have the exit race - the TUI keeps the pane open:

```bash
tmux split-window -d -P -F '#{pane_id}' -t "$TMUX_PANE" \
  "HERDLET_ID=worker $LAUNCH -n 'worker: <purpose>' --model <cheap-id>"
```

**always pass `-t "$TMUX_PANE"`.** without a target, `split-window` splits
the window the human is LOOKING AT right now, not yours - if they switched to
another project's window while you worked, your worker lands in that window
and they lose track of it. `-t "$TMUX_PANE"` splits your own pane, wherever
the human's focus is. (`herdlet spawn` does this for you.)

`herdlet spawn` keeps the master in the left half and stacks workers in the
right half. it gives the right workers equal heights. a window under 160
columns or a stack below `--min-height 12` puts the worker in a new window.
pass `--vertical` to use the old explicit vertical split.
spawn JSON reports `placement` as `right-stack`, `new-window`, or `vertical`.

after they register you drive them with `send` / `wait` / `peek` cycles.

**always launch a worker with an explicit model - never let it inherit the
default.** a herdlet worker is its own top-level session, not a subagent, so it
runs on `$LAUNCH`'s MAIN (priciest) model unless you say otherwise - that is how
a top-tier master ends up spawning top-tier workers and burns the budget fast.
downgrade mechanical roles explicitly, with a model id valid for YOUR setup:

| tier | role | Claude Code | Claude Code on Fireworks |
|---|---|---|---|
| cheap | test runners, seeders, formatters | `--model haiku` | `--model accounts/fireworks/models/minimax-m3` |
| mid | implementers | `--model sonnet` | bare `$LAUNCH` (main = glm-5p2) |
| top | hard thinking (usually you) | `--model opus` | bare `$LAUNCH` (main = glm-5p2) |

high effort/thinking settings multiply output tokens on every turn of that
worker's life, so reserve them for genuinely hard design work, never for
mechanical roles.

Claude Code 2.1.263 can show `medium` in its footer although herdlet passes a different `--effort` value.

**provision permissions at spawn time.** an unattended worker that hits a
permission menu just sits there until someone presses a key; a worker that
prompts on every shell command turns you into a full-time babysitter. make
the menus not appear, using your harness's own permission mechanism.

`herdlet spawn --allow "pnpm test"` pre-seeds one command prefix. repeat the
flag for each prefix. Claude uses `Bash(<prefix>:*)` entries. Codex uses
[`prefix_rule`](https://learn.chatgpt.com/docs/agent-configuration/rules.md)
entries in the trusted project layer.

for Claude Code:

- pre-seed the allowlist in the worker's cwd before spawning: add the command
  shapes the role will need (`Bash(pnpm *)`, `Bash(docker *)`, ...) to
  `.claude/settings.local.json` under `permissions.allow`
- disposable worktree or sandbox: `--permission-mode bypassPermissions` is
  fine when the blast radius is contained
- otherwise scope at launch: `--allowedTools "Bash(pnpm *)" "Bash(git diff *)"`

(`--permission-mode acceptEdits` only auto-allows file edits; every shell
command still prompts.) other harnesses have their own allowlist/sandbox
flags - check `$LAUNCH --help`. answering menus by hand (see "unblock a
worker") is the exception path, not the loop.

for Codex, `--sandbox danger-full-access --approval never` is equivalent to
Claude's `bypassPermissions`. it gives the worker no prompts and no sandbox.
git is the only guard. use this combination only in a disposable worktree or a
repository you can reset.

**pre-registration blind spot (hand-built panes only).** keep the pane id
`split-window -P` printed you; until the worker's first hook event it has no
registry entry at all, so it is only addressable by that pane id. a first run in
a new directory can block on a trust/onboarding prompt BEFORE any hook exists -
`peek` / `approve` that worker by pane id (`%N`), not by the name you gave it.
`herdlet spawn` has no such gap: it registers the worker as `spawning` the
moment the pane exists.

**brief your workers on cwd.** commands run from the worker's own cwd; if you
tell it to `cd X && ...` for another repo, that prefix defeats prefix-based
permission allowlists and adds an extra approval warning per command. tell it
to use `git -C <path>` (or the tool's own `--cwd`/`-C` flag) instead.

**keep the worker's pane readable.** if the worker runs a full-screen TUI on
the terminal's ALTERNATE screen buffer (Claude Code's `tui: fullscreen`, like
vim/htop), its transcript lives in that buffer, NOT the pane's native
scrollback - so `peek`, `approve`, and `wait --match` (which read
`tmux capture-pane -S`) get stale pre-launch scrollback plus at most the
current screenful, never the conversation history, and you can silently misread
or miss what a worker is showing. launch workers in the inline/classic
renderer: for Claude Code prepend `CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` (or
set `"tui": "default"` in the worker cwd's `.claude/settings.json`, which
overrides your user setting). state DETECTION is unaffected either way - it is
hook-driven, not scraped - this only restores your ability to READ the pane.

## act as a master orchestrator

if the user asks you to manage a project (or several), you are the master:
a long-lived agent in window 0 of a domain session. per project, create one
window with one pane per role, then relay work between them:

```bash
tmux new-window -t personal -n herdlet -c ~/code/herdlet
herdlet spawn --id personal/herdlet/dev    --model <mid-id>   --effort medium --brief plans/dev.md
herdlet spawn --id personal/herdlet/tester --model <cheap-id> --effort low    --brief plans/tester.md
```

`spawn` already sets `CC_IMESSAGE_SKIP=1` (so only masters page the human) and
`CLAUDE_CODE_DISABLE_ALTERNATE_SCREEN=1` (so the pane stays readable), and waits
for each worker to register. when you build a pane by hand, set those yourself,
plus the mute env vars of any other per-turn notification hooks the user runs.
the reverse also exists: `HERDLET_SKIP=1` makes herdlet ignore a nested
agent run entirely; set it when a hook or script of yours shells out to a
nested agent (`claude -p`, `codex exec`) from inside an agent's pane
environment.

then loop: `send` a role its task, one long `wait --state done,blocked,limited`
on all roles at once (`--id a,b` or `--prefix proj/`), `peek` for the outcome,
pass results to the next role, report to the user.
relay `peek` summaries, not whole transcripts, to keep your own context small.
`pair` the implementer with its tester or reviewer (see "talk to a peer
directly") so their mechanical rounds stop costing you a turn each; you still
read the outcome in the topic file and in their reports.
after collecting a worker's result, `herdlet ack --id <worker>` clears it from
the inbox: a `done` (still-alive) worker flips back to `idle`, an `ended` (dead)
one is removed. then `list` reads as an inbox of live work.
pass `--kill-pane` to `ack` or `remove` to close a finished pane or a stale shell
pane. the command leaves a fresh wrapper worker open. pass `--force` to override
this guard.
switching projects means a new window; leave finished windows alive so the
user can inspect them.

**collect a worktree worker's diff atomically, before any cleanup.** if a worker
ran in an isolated git worktree, snapshot everything it produced in one shot -
`git -C <wt> add -A && git -C <wt> diff --cached` (plus copy any files you need)
- BEFORE you `git checkout`/`clean`/reset that worktree for the next worker. a
hand-rolled loop over `git status --porcelain` will trip over untracked
directories and a premature `clean` can delete a deliverable you never captured.

**watch `compacts` and hand over before it climbs.** `herdlet get` shows
`compacts`, how many times an agent has compacted its context (its state and
message are untouched by a compaction). `watch` emits a `compacted` event, and
`list` adds `C<n>` to the state. a worker on its second compaction has
already lost detail and is paying to re-read what it forgot; that is the signal to end
its phase, have it write a handover file, and start the next phase with a fresh
worker rather than nursing it along. your own compactions count too: keep the
herd's state in `plans/*.md`, not in your context, so a compaction costs you
nothing.

**keep workers short-lived.** a worker that lives for hours drags an
ever-growing context into every one of its turns; the tail of a fat session
is its most expensive stretch. prefer one worker per phase or milestone: it
reads a brief file, does its slice, reports, and is retired; the next phase
gets a fresh worker. hand phases over through brief files on disk
(`plans/*.md`), not through a long-lived worker's memory.

**heavy fan-out skills are budget events.** skills that spawn many subagents
at once (multi-agent code review, research harnesses) run every subagent on
the calling session's model and count against its usage limits. NEVER run an
out-of-the-box code-review slash command from a herd session: at high effort
it bursts 8+ finder subagents on the master's expensive model in one shot -
enough to trip a session limit and stall the whole herd. review herd-natively
instead: spawn one-shot reviewer workers on cheap models that read
pre-gathered context from disk, then synthesize their findings yourself (if a
distilled herd review skill is installed - e.g. ponytail-review - use it).
if a limit does kill subagents mid-flight, resume them after the reset
instead of respawning; a respawned agent redoes all of its work.

## unblock a worker (questions and permission prompts)

a `blocked` worker is sitting on a permission menu; a `done` worker may have
ended its turn by asking you something. either way `peek` first, then:

- **question in plain text**: answer it like a user would:
  `herdlet send --id herdlet/dev "yes, proceed with both releases"`
- **permission menu** (numbered options): `approve` selects `--choice yes` by
  default. use `--choice always` for a matching don't-ask-again option, or
  `--choice no` to deny. menus react to a bare keypress; `send` would append
  Enter. `--wait` is the primary form. one call answers and marks the worker
  `working`. then it waits for the next real transition and shows the pane.
  the plain wait also returns a state that arrived during the settle period:

  ```bash
  herdlet approve --id herdlet/dev --wait                  # one-time Yes, then wait+peek
  herdlet approve --id herdlet/dev --choice always --wait  # matching don't-ask-again choice
  herdlet approve --id herdlet/dev --choice no --wait      # deny, then wait+peek
  tmux send-keys -t %5 Escape                           # dismiss a dialog
  ```

if an `always` option is absent, `approve` selects the one-time Yes option and
writes a note. on a Codex trust menu, `yes` and `always` both select option 1.
use `--option N` only as a raw escape hatch. Codex menu lengths vary, so the
same digit can approve one menu and deny another. for Codex, `always` suppresses
only the exact command prefix that the menu shows.

`approve` scans the full visible pane before it types. without a supported
menu, it exits 5 and shows the last five non-empty lines. it also changes a
stale `blocked` record to `working` because another user answered the menu.

rules of thumb: approve only what matches the task you assigned; deny with a
follow-up instruction if the action looks off-task; escalate to the human
instead of guessing on anything destructive, irreversible, or outward-facing
(pushes, PR creation, publishes, deletes) - milestones in a plan or handoff
doc describe the goal, not permission to do these yourself. **answer first,
peek once - never peek inside a poll loop**; `approve --wait` already gives
you that in one call. without `--wait`, a denied permission fires no hook,
so after answering a menu re-check with `get` rather than `wait`.

if you are approving the same class of command over and over, stop: that is a
provisioning failure, not a babysitting duty. pick the menu's "always allow"
option, or add the command shape to the worker's `.claude/settings.local.json`
allowlist, and get out of the loop (see "provision permissions at spawn
time"). reserve one-off approvals for commands that genuinely warrant
case-by-case judgment.

## resume a dead worker

agent hooks record each agent's native session ref (`session` in `get`).
when a worker's process dies without a hook firing - crash, usage limit,
accidental ctrl-c - its pane drops back to a shell and `list` shows the
agent as `stale`. do NOT respawn from scratch: a respawned agent redoes all
of its work, a resumed one continues with its context intact.

```bash
herdlet resume --id gtax/impl             # types the native resume command (claude --resume / codex resume / opencode --session)
herdlet resume --id gtax/impl --pane %7   # pane died too: spawn a fresh one, resume there
```

a session that exited cleanly shows `ended` (not `stale`) but resumes the same
way - the record and its session ref are kept until you `remove`/`ack` it.

`resume` refuses to type into a pane that is running something other than a
bare shell (`--force` overrides). after the agent comes back, `send` it a
short "where were we" nudge so it re-anchors and continues.

## report state manually

only needed for agents/processes without hook integration:

```bash
herdlet report --id deploy --state working --message "rolling out"
herdlet report --id deploy --state done
```

## stream events

```bash
herdlet watch                 # all state changes, JSON lines
herdlet watch --id builder    # one agent
herdlet watch --state blocked # who just got stuck
```

## caveats

- after `send`, sleep ~2s before `wait`: the target flips to `working` via its
  own hook, which takes a moment. waiting instantly can match its stale `done`.
- ids are live registry entries; re-read `herdlet list` rather than assuming
  an old id still exists. the registry self-cleans - finished records drop
  after 24h, and anything untouched for ~3 days is dropped regardless of state
  (`HERDLET_MAX_AGE`), re-registering only when the agent next acts.
- `send` is terminal input: no control sequences. multi-line and long text is
  fine (delivered as a bracketed paste); use `--file` for anything big.
- waiting only on `done` can hang forever if the target hits a permission
  prompt or a usage limit; include `blocked,limited`.
- `limited` is scraped from the pane, not pushed by a hook, so it lands within
  ~30s (a worker that just reported waits one more tick) and only for workers
  whose pane is readable (see the renderer note in "keep the worker's pane
  readable"). it is a hint that the worker is parked, never a reason to
  respawn it.
- a denied permission interrupts the turn without firing any hook, so
  `blocked` can linger until the target's next event. an old `blocked` with a
  quiet pane means a human already acted; `peek` before trusting it. if the
  agent process itself died, `list` shows `stale` instead - that one needs
  `resume`, not a keypress.
- the daemon persists the registry next to its socket and reloads it on
  restart, so records survive; right after a restart they may be a beat
  stale until the next hook event - the `gone`/`stale` annotations in
  `list` still tell you what is real.
