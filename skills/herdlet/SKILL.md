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

**states**: `idle`, `working`, `blocked`, `done` (custom strings allowed).
`blocked` means the agent is waiting for a human approval; `done` means its
turn finished. states update automatically via Claude Code / Codex hooks, you
normally never report your own state.

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

a state of `gone` means the agent's pane no longer exists. a state of
`stale` means the pane is back at a bare shell while the last hook said
working/blocked - the agent process died (crash, usage limit, ctrl-c)
without a hook firing. see "resume a dead worker".

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
whichever transitions first (`result.id` says which):

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

```bash
tmux split-window -d -P -F '#{pane_id}' \
  "HERDLET_ID=worker claude --model sonnet -p 'run the tests and summarize failures'"
herdlet wait --id worker --state done,blocked --timeout 550
herdlet peek --id worker --lines 40
```

interactive workers are the same without `-p`; after they register you drive
them with `send` / `wait` / `peek` cycles.

**pick a model and effort per role - never spawn bare `claude`.** a worker
without `--model` inherits the human's default model, often their most
expensive tier; a herd of those burns tokens fast. cheap tiers for mechanical
roles (test runners, seeders, formatters), a mid tier for implementers, top
tier only where the hard thinking happens (usually you, the coordinator).
high effort/thinking settings multiply output tokens on every turn of that
worker's life, so reserve them for genuinely hard design work, never for
mechanical roles.

**provision permissions at spawn time.** an unattended worker that hits a
permission menu just sits there until someone presses a key; a worker that
prompts on every shell command turns you into a full-time babysitter. make
the menus not appear:

- pre-seed the allowlist in the worker's cwd before spawning: add the command
  shapes the role will need (`Bash(pnpm *)`, `Bash(docker *)`, ...) to
  `.claude/settings.local.json` under `permissions.allow`
- disposable worktree or sandbox: `--permission-mode bypassPermissions` is
  fine when the blast radius is contained
- otherwise scope at launch: `--allowedTools "Bash(pnpm *)" "Bash(git diff *)"`

note `--permission-mode acceptEdits` only auto-allows file edits; every shell
command still prompts. answering menus by hand (see "unblock a worker") is
the exception path, not the loop.

**pre-registration blind spot.** keep the pane id `split-window -P` printed
you; until the worker's first hook event it has no registry entry at all, so
it is only addressable by that pane id. a first run in a new directory blocks
on the folder-trust dialog BEFORE any hook exists - `peek` / `approve` that
worker by pane id (`%N`), not by the name you gave it.

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
tmux send-keys -t personal:herdlet.0 "HERDLET_ID=personal/herdlet/dev CC_IMESSAGE_SKIP=1 claude --model sonnet" Enter
tmux send-keys -t personal:herdlet.1 "HERDLET_ID=personal/herdlet/tester CC_IMESSAGE_SKIP=1 claude --model haiku" Enter
herdlet wait --id personal/herdlet/dev --state idle --timeout 30   # registered?
```

spawn workers with the mute env vars of any per-turn notification hooks the
user runs (like `CC_IMESSAGE_SKIP=1` above), so only masters page the human.
the reverse also exists: `HERDLET_SKIP=1` makes herdlet ignore a nested
agent run entirely; set it when a hook or script of yours shells out to
`claude -p` from inside an agent's pane environment.

then loop: `send` a role its task, one long `wait --state done,blocked` on
all roles at once (`--id a,b` or `--prefix proj/`), `peek` for the outcome,
pass results to the next role, report to the user.
relay `peek` summaries, not whole transcripts, to keep your own context small.
after collecting a worker's result, `herdlet ack --id <worker>` flips its
`done` back to `idle` - then `list` reads as an inbox: `done` means results
you have not collected yet.
switching projects means a new window; leave finished windows alive so the
user can inspect them.

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
herdlet resume --id gtax/impl             # types `claude --resume <session>` into its pane
herdlet resume --id gtax/impl --pane %7   # pane died too: spawn a fresh one, resume there
```

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
  an old id still exists.
- `send` is terminal input: no control sequences. multi-line text is fine
  (delivered as a bracketed paste).
- waiting only on `done` can hang forever if the target hits a permission
  prompt; include `blocked`.
- a denied permission interrupts the turn without firing any hook, so
  `blocked` can linger until the target's next event. an old `blocked` with a
  quiet pane means a human already acted; `peek` before trusting it. if the
  agent process itself died, `list` shows `stale` instead - that one needs
  `resume`, not a keypress.
- if the daemon was restarted, agents re-register on their next hook event;
  a missing entry does not necessarily mean the pane is dead. `peek` it.
