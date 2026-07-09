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
address it.

**your own id**: `$HERDLET_ID` if set, else `$TMUX_PANE`.

## discover the herd

```bash
herdlet list          # table: id, state, age, agent, pane, where, message
herdlet list --json   # same, machine-readable
herdlet get --id builder
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
