---
name: pipeline
description: How to work inside a delivery-loop run. Use when a turn starts with "Stage <n>/<N>:", when a run is paused or blocked by delivery-loop, or when asked how the loop expects a stage to end.
---

# Working inside a delivery-loop run

A delivery-loop run drives one task through a fixed list of stages, one after
another, without a person in the seat. The plugin's hooks read how each of your
turns ends and decide what happens next. This skill is the contract.

## A stage

Each stage starts with a message that begins `Stage <n>/<N>: <name>. Task: ...`.
It may name a command to run, then gives the stage's instructions. Do the
stage's work across as many turns as it takes.

## Ending a stage

End the last turn of a stage with exactly one of these as the final line:

- `<promise>STAGE DONE</promise>`: the stage is finished. If the stage says it
  must write something first (`PLAN: <path>`, `PR: <url>`), write that line
  before the token. A stage that needs a clean worktree is only accepted when
  everything is committed.
- `<promise>NEEDS HUMAN</promise>`: you need a person's decision. Put a decision
  card right above the token:

  ```
  ASK   The one question, in a sentence.
  REC   1. The option you recommend
  ALT   2. The alternative   3. Another
  COST  What each option costs, in a line.
  REPLY 1 | 2 | 3
  ```

  The run pauses, and the card is what the person reads.

A turn that ends without a token is sent back with the stage's instructions. A
few turns in a row without one pause the run for a person.

## Waiting for background work

If you started background work (an agent, or a command run in the background),
end your turn without a token, for example with one line saying what you wait
for. The loop waits for it, and its notification wakes you. Do not wait in the
shell: a foreground `sleep`, `wait`, polling loop or `tail -f` is denied. Write
`<promise>STAGE DONE</promise>` only once every background task has reported or
been stopped with `TaskStop`. A background command counts as waited on only in
the turn that started it; an agent counts until it reports. A command that
never ends on its own, such as a dev server, never wakes you: stop it with
`TaskStop` before you end a turn to wait, or the run waits until a person looks.
(`TaskStop` is Claude Code's name; a loop.toml can name another harness's tool
as `stop_task_tool`, and the loop's refusal text names it.)

## What the loop guards

- Some files are enforcement files: `loop.toml`, `.git`, and
  `.claude/settings.json` and `.claude/settings.local.json`. Edits to them are
  denied. So are edits to the plugin, to `~/.claude` and to the loop's state.
- Loop files, such as a stage prompt override, can be edited only when the
  approved plan lists them in a ```loop-edits block.
- A shell edit to a guarded file is noticed at the end of the turn, and the run
  pauses for a person.
- `git push` is allowed in the stage that opens the pull request, for this
  run's branch, to `origin`, never forced.
- `delivery-loop resume` and `delivery-loop abort` are for a person only, and
  refuse when you run them. If the run needs either, stop and say so.

## Commands

`delivery-loop status` shows the run's stage, its pause reason and its last
events. A person uses `/delivery-loop:pipeline resume` or
`/delivery-loop:pipeline abort`.
