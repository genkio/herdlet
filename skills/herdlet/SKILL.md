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

**states**: `idle`, `working`, `blocked`, `done`, `ended` (custom strings
allowed). `blocked` means the agent is waiting for a human approval; `done`
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

## wait for another agent

```bash
herdlet wait --id builder --state done --timeout 600
```

always include `blocked` in the states unless you specifically want to sleep
through approval prompts: a blocked agent will not finish until a human acts.

```bash
herdlet wait --id builder --state done,blocked --timeout 600
```

exit code 0 = state reached (`result.state` says which), 2 = timeout. always
pass `--timeout`. after waking, `peek` to see what actually happened.

to watch several agents at once, wait on all of them in one call; it wakes on
whichever transitions first (`result.id` says which). `result.matched` lists
EVERY agent already in a target state at wake time, so collect that whole
batch and only re-wait for the stragglers, instead of one wait per agent:

```bash
herdlet wait --id proj/dev,proj/tester --state done,blocked --timeout 550
herdlet wait --prefix proj/ --state blocked --timeout 550   # anyone stuck?
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
```

this is that pane's visible scrollback tail, exactly what a human would see.
pass `--join` to unwrap soft-wrapped lines - better when grepping logs or
transcripts.

## send instructions to another agent

```bash
herdlet send --id builder "run the full test suite and report failures"
```

the text is typed into that agent's terminal and submitted with Enter, as if
its human had typed it. if the target agent is mid-turn, the message queues
like normal user input. use `--no-enter` to type without submitting.
multi-line text is delivered as one bracketed paste, so embedded newlines
read as text instead of submitting early.

## spawn a worker agent

spawn a worker with the **same launch command you were started under**, not a
bare vendor binary. that command carries your model routing, endpoint/auth env,
and per-role config; a bare `claude`/`codex` in a fresh pane inherits none of it
and may hit the wrong endpoint or an unconfigured model. call it `$LAUNCH` below
and substitute your own:

| harness | `$LAUNCH` |
|---|---|
| Claude Code | `claude` |
| Claude Code via a custom endpoint (a wrapper you wrote that sets base url / key / model) | that wrapper |
| Codex | `codex` |
| opencode | `opencode` (the `herdlet setup` plugin reports its state) |

a bare `$LAUNCH -p` pane CLOSES the moment the agent exits, destroying its
scrollback - by the time you peek, the result is gone. wrap a one-shot worker
in a shell that keeps the pane alive and emits its own done-marker AFTER the CLI
returns:

```bash
tmux split-window -d -P -F '#{pane_id}' "bash -c '\
  HERDLET_ID=proj/worker $LAUNCH --model <cheap-id> \
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
tmux split-window -d -P -F '#{pane_id}' \
  "HERDLET_ID=worker $LAUNCH --model <cheap-id>"
```

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

**provision permissions at spawn time.** an unattended worker that hits a
permission menu just sits there until someone presses a key; a worker that
prompts on every shell command turns you into a full-time babysitter. make
the menus not appear, using your harness's own permission mechanism. for
Claude Code:

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

**pre-registration blind spot.** keep the pane id `split-window -P` printed
you; until the worker's first hook event it has no registry entry at all, so
it is only addressable by that pane id. a first run in a new directory can
block on a trust/onboarding prompt BEFORE any hook exists - `peek` / `approve`
that worker by pane id (`%N`), not by the name you gave it.

**brief your workers on cwd.** commands run from the worker's own cwd; if you
tell it to `cd X && ...` for another repo, that prefix defeats prefix-based
permission allowlists and adds an extra approval warning per command. tell it
to use `git -C <path>` (or the tool's own `--cwd`/`-C` flag) instead.

## act as a master orchestrator

if the user asks you to manage a project (or several), you are the master:
a long-lived agent in window 0 of a domain session. per project, create one
window with one pane per role, then relay work between them:

```bash
tmux new-window -t personal -n herdlet -c ~/code/herdlet
tmux split-window -h -t personal:herdlet
tmux send-keys -t personal:herdlet.0 "HERDLET_ID=personal/herdlet/dev CC_IMESSAGE_SKIP=1 $LAUNCH --model <mid-id>" Enter
tmux send-keys -t personal:herdlet.1 "HERDLET_ID=personal/herdlet/tester CC_IMESSAGE_SKIP=1 $LAUNCH --model <cheap-id>" Enter
herdlet wait --id personal/herdlet/dev --state idle --timeout 30   # registered?
```

spawn workers with the mute env vars of any per-turn notification hooks the
user runs (like `CC_IMESSAGE_SKIP=1` above), so only masters page the human.
the reverse also exists: `HERDLET_SKIP=1` makes herdlet ignore a nested
agent run entirely; set it when a hook or script of yours shells out to a
nested agent (`claude -p`, `codex exec`) from inside an agent's pane
environment.

then loop: `send` a role its task, one long `wait --state done,blocked` on
all roles at once (`--id a,b` or `--prefix proj/`), `peek` for the outcome,
pass results to the next role, report to the user.
relay `peek` summaries, not whole transcripts, to keep your own context small.
after collecting a worker's result, `herdlet ack --id <worker>` clears it from
the inbox: a `done` (still-alive) worker flips back to `idle`, an `ended` (dead)
one is removed. then `list` reads as an inbox of live work.
switching projects means a new window; leave finished windows alive so the
user can inspect them.

**collect a worktree worker's diff atomically, before any cleanup.** if a worker
ran in an isolated git worktree, snapshot everything it produced in one shot -
`git -C <wt> add -A && git -C <wt> diff --cached` (plus copy any files you need)
- BEFORE you `git checkout`/`clean`/reset that worktree for the next worker. a
hand-rolled loop over `git status --porcelain` will trip over untracked
directories and a premature `clean` can delete a deliverable you never captured.

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
- **permission menu** (numbered options): `approve` presses the option key
  (menus react to a bare keypress; `send` would append Enter). `--wait` is
  the primary form: one call answers, marks the worker `working`, waits for
  its next real transition (edge-waited, so it can't match the stale
  pre-answer state), and shows the pane - the whole babysit cycle in one
  shot instead of three hand-rolled calls:

  ```bash
  herdlet approve --id herdlet/dev --wait               # option 1: approve once, then wait+peek
  herdlet approve --id herdlet/dev --option 3 --wait    # deny, then wait+peek; follow up with `send` if off-task
  tmux send-keys -t %5 Escape                           # dismiss a dialog
  ```

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
- `send` is terminal input: no control sequences. multi-line text is fine
  (delivered as a bracketed paste).
- waiting only on `done` can hang forever if the target hits a permission
  prompt; include `blocked`.
- a denied permission interrupts the turn without firing any hook, so
  `blocked` can linger until the target's next event. an old `blocked` with a
  quiet pane means a human already acted; `peek` before trusting it. if the
  agent process itself died, `list` shows `stale` instead - that one needs
  `resume`, not a keypress.
- the daemon persists the registry next to its socket and reloads it on
  restart, so records survive; right after a restart they may be a beat
  stale until the next hook event - the `gone`/`stale` annotations in
  `list` still tell you what is real.
