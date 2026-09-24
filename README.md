# delivery-loop

MIT licensed. Copyright (c) 2026 crblabs. Free to use, copy, modify and
distribute. See `LICENSE`.

## What this is

The delivery loop is a state machine that drives one coding-agent task through
a declared sequence of stages, in order. The default sequence is the five the
loop shipped with:

1. autoplan
2. implement
3. qa
4. review
5. ship

The sequence is configuration, not code. A host repository that works another
way writes its own stages in `loop.toml` and the loop drives those instead. A
stage declares the contract the loop depends on, not only a name: the command it
invokes, the prompt it is given, what it leaves for the next stage, whether it
must end over a clean worktree, whether it stops for a person, and whether it
needs the agent sandbox off. `templates/loop.toml` writes the five defaults out
in full.

The loop advances a run from stage to stage without a person in the seat. When
a decision needs a human, the loop pauses and records why. A supervisor watches
every run at once and escalates each pause to the operator.

This repository is the harness-neutral home of that loop. "Harness" means the
coding-agent runtime the loop rides on. The core knows nothing about any
particular harness. An adapter translates one harness to the core.

## Status

The code has not moved here yet. The loop exists today inside another
repository and will be lifted out later. What is in this repository now is the
shape and the contracts: directory boundaries, the adapter contract, and the
per-repo configuration seam. Nothing here should be read as a description of
shipped code.

Where a detail is not yet settled, this repository says "unsettled" rather than
guessing.

## What a harness adapter must provide

An adapter must supply four capabilities. Three are required. One is optional.

1. **A turn-end event that the adapter can block and reinject into.** The
   harness must fire an event when the agent finishes a turn, must let the
   adapter block that turn from ending, and must let the adapter inject text
   back into the session. This is how the loop advances a stage.
2. **A pre-tool check that can deny a command.** The harness must call the
   adapter before a tool runs, and must honour a denial. This is how the loop
   enforces guards.
3. **A session identity.** The harness must give the adapter a stable
   identifier for the session, so a run can be tied to a session and back.
4. **A way to read the session's pending question.** Optional. When the harness
   can tell the adapter what the agent is currently asking the operator, the
   supervisor can escalate the question itself. When the harness cannot, the
   loop degrades to state-file-only: the supervisor escalates what the state
   file records, and nothing more.

## The hard requirement

**A harness without a blocking turn-end event cannot run the loop at all.**
There is no fallback and no degraded mode for this one. Blocking the end of a
turn is the mechanism the loop is built on. A harness that fires a turn-end
event it will not let the adapter block is not supported.

Capability 2 and capability 3 are also required, but they are boundaries rather
than the engine. Capability 4 is the only one that degrades.

## Layout

| Directory | Holds |
|---|---|
| `core/` | The stage machine and everything harness-neutral. |
| `adapters/claude_code/` | The adapter for one harness. |
| `prompts/` | The stage prompts, as harness-neutral text. |
| `templates/` | `loop.toml`, the per-repo configuration a host repo fills in. |
| `docs/` | The contract and the operator runbooks. |

Each directory carries a `README.md` saying what belongs in it and what does
not.
