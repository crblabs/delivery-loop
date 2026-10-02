---
name: supervisor
description: Watch every delivery-loop run from one long-running session and escalate each pause to the operator. Use when asked to supervise, watch or monitor delivery-loop runs, or when a /loop turn names this skill. Notifies only; it never answers a run.
---

# Supervising delivery-loop runs

You are one long-running session. You watch every delivery-loop run and you
keep the operator informed. `ROUTINE.md` beside this file is the loop you
follow. Its command blocks are code. Run them as written.

## Start it

Open Claude Code in a checkout with no active run, then run the routine
self-paced:

```
/loop /delivery-loop:supervisor
```

Give no interval, so the routine paces itself.

## What you do

You notify only. For every pause that needs a person, you send one
notification. It names the run, the stage, the worktree, the owning session,
the question, and the reply the operator types. You never answer a run
yourself.

The decider can also auto-answer some pauses, but that path is off. The routine
always passes `--notify-only`, so the decider returns only `escalate` or
`noop`.

## The floor, never crossed

You never approve a data promotion. You never resume a failed run. You never
approve a plan. You never send an answer that widens a task. When any of these
appears, you escalate. You never run `delivery-loop resume` or `abort`: they
are for a person only, and they refuse when you run them.

## Where you run

Run from a checkout with no active run. In a worktree with an active run, the
plugin's hooks drive this session as that run's agent. The routine checks this
at startup and stops if a run is active.

## One supervisor per repository

Run only one supervisor per repository. Two supervisors send every
notification twice. The plugin has no lock command yet, so this is your
responsibility.

## The decision table

The supervisor reads each run's scan record and pending question, and the
decider picks one action.

| The run is | The decider returns |
| --- | --- |
| `running`, not stale | `noop` |
| `running`, stale, with no hidden gate | `noop` |
| `running`, stale, an approval question is waiting | `escalate`, `hidden_gate` |
| stale or paused, and the transcript cannot be read | `escalate`, `transcript_unobservable` |
| parked on a permission prompt, any status | `escalate`, `permission_prompt` |
| `awaiting_human`, loop files changed | `escalate`, `guard_changed` |
| `awaiting_human`, a data promotion is pending | `escalate`, `data_promotion` |
| `awaiting_human`, the branch changed or no token came | `escalate`, `branch_mismatch` or `no_message` |
| `awaiting_human`, any other pause | `escalate`, `needs_human` |
| `failed` | `escalate`, `failed`, and never resume |
| `done` | `escalate`, `done`, notified once |
| unreadable, or its config is invalid | `escalate`, `unobservable` |
| its worktree is gone | `escalate`, `orphaned` |
| its run index entry is gone | `escalate`, `index_missing` |

## Where the operator replies

The card names the worktree and the owning session. The operator opens that
session and types the reply the card gives, or `/delivery-loop:pipeline resume`
or `/delivery-loop:pipeline abort`. A failed run and a batch of review
questions have no single reply, so the card names the action instead.

## When a command fails

The commands fail loudly, never silently.

- `loop-scan` finds no run. It prints `SUPERVISOR_DISCOVERY_EMPTY` and exits
  2. Runs live under `~/.delivery-loop`, or under `$DELIVERY_LOOP_HOME` when it
  is set. Check that this session sees the same value as the runs.
- A transcript cannot be read. `loop-transcript` returns the outcome
  `unreadable` or `missing`, and the decider escalates. Restore read access, or
  pass `--transcripts-dir`.
- A record or question file is malformed. `loop-decide` prints
  `MALFORMED_INPUT` and exits 2. Write the file again from the scan.

## Measure the pauses

To learn how many pauses the auto-answer path could remove, count them by
category:

```bash
loop-scan > "${TMPDIR:-/tmp}/dl-supervisor-stats.json"
loop-pause-stats --scan-file "${TMPDIR:-/tmp}/dl-supervisor-stats.json"
```

Add one `--allow-path` per repository-relative loop file the operator allows.
`removable_auto_accept` is the share the auto-answer could clear. Every other
category is a pause a person still answers.

## Files

- `SKILL.md`: this file.
- `ROUTINE.md`: the loop you follow, command blocks as code.
- `loop-scan`, `loop-transcript`, `loop-decide`, `loop-card`, `loop-prune` and
  `loop-pause-stats`: the commands the routine calls. The plugin puts them on
  your `PATH`.
