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

a state of `gone` means the agent's pane no longer exists.

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

if your shell tool has its own timeout (Claude Code's Bash defaults to 2
minutes), wait in chunks instead of one long block:

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

## send instructions to another agent

```bash
herdlet send --id builder "run the full test suite and report failures"
```

the text is typed into that agent's terminal and submitted with Enter, as if
its human had typed it. if the target agent is mid-turn, the message queues
like normal user input. use `--no-enter` to type without submitting.

## spawn a worker agent

```bash
tmux split-window -d -P -F '#{pane_id}' \
  "HERDLET_ID=worker claude -p 'run the tests and summarize failures'"
herdlet wait --id worker --state done,blocked --timeout 900
herdlet peek --id worker --lines 40
```

interactive workers are the same without `-p`; after they register you drive
them with `send` / `wait` / `peek` cycles.

## act as a master orchestrator

if the user asks you to manage a project (or several), you are the master:
a long-lived agent in window 0 of a domain session. per project, create one
window with one pane per role, then relay work between them:

```bash
tmux new-window -t personal -n herdlet -c ~/code/herdlet
tmux split-window -h -t personal:herdlet
tmux send-keys -t personal:herdlet.0 "HERDLET_ID=personal/herdlet/dev CC_IMESSAGE_SKIP=1 claude" Enter
tmux send-keys -t personal:herdlet.1 "HERDLET_ID=personal/herdlet/tester CC_IMESSAGE_SKIP=1 claude" Enter
herdlet wait --id personal/herdlet/dev --state idle --timeout 30   # registered?
```

spawn workers with the mute env vars of any per-turn notification hooks the
user runs (like `CC_IMESSAGE_SKIP=1` above), so only masters page the human.
the reverse also exists: `HERDLET_SKIP=1` makes herdlet ignore a nested
agent run entirely; set it when a hook or script of yours shells out to
`claude -p` from inside an agent's pane environment.

then loop: `send` a role its task, chunked `wait --state done,blocked`,
`peek` for the outcome, pass results to the next role, report to the user.
relay `peek` summaries, not whole transcripts, to keep your own context small.
switching projects means a new window; leave finished windows alive so the
user can inspect them.

## unblock a worker (questions and permission prompts)

a `blocked` worker is sitting on a permission menu; a `done` worker may have
ended its turn by asking you something. either way `peek` first, then:

- **question in plain text**: answer it like a user would:
  `herdlet send --id herdlet/dev "yes, proceed with both releases"`
- **permission menu** (numbered options): menus react to a single keypress,
  so use tmux directly; `send` would append Enter:

  ```bash
  tmux send-keys -t %5 1        # approve once (pane id from herdlet list)
  tmux send-keys -t %5 3        # deny; then `send` a corrective instruction
  tmux send-keys -t %5 Escape   # dismiss a dialog
  ```

rules of thumb: approve only what matches the task you assigned; deny with a
follow-up instruction if the action looks off-task; escalate to the human
instead of guessing on anything destructive, irreversible, or outward-facing
(pushes, publishes, deletes). avoid "always allow" menu options unless the
human said so, they persist beyond this task. a denied permission fires no
hook, so after answering a menu re-check with `get` rather than `wait`. for
trusted bulk work, cut the prompt noise at spawn time instead:
`claude --permission-mode acceptEdits`.

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
- `send` is terminal input: keep it one line, no control sequences needed.
- waiting only on `done` can hang forever if the target hits a permission
  prompt; include `blocked`.
- a denied permission interrupts the turn without firing any hook, so
  `blocked` can linger until the target's next event. an old `blocked` with a
  quiet pane means a human already acted; `peek` before trusting it.
- if the daemon was restarted, agents re-register on their next hook event;
  a missing entry does not necessarily mean the pane is dead. `peek` it.
