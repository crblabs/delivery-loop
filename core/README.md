# core

The harness-neutral half of the delivery loop. Everything in here is written
against the loop's own vocabulary, never a harness's.

## What belongs here

- **The stage machine.** The five stages, the legal transitions between them,
  and the rules that decide when a stage is done.
- **The state file schema.** The on-disk record of one run: which stage it is
  in, what it has done, why it is paused. The schema, its version, and the read
  and write path.
- **Guard hashing.** The way a guard turns a proposed command into a stable
  hash, so an approval applies to one command and not to a family of them.
- **Loop-path classification.** The rule that decides whether a given path is
  part of the loop's own machinery or part of the host repository's work.
- **Carve-outs.** The declared exceptions to a guard: paths or actions the loop
  is allowed to touch that it would otherwise be denied.
- **The pause taxonomy.** The closed set of reasons a run may pause, and what
  the operator is being asked in each case.
- **The decision card.** The structured summary handed to a human at a pause:
  what happened, what is being asked, what the options are.
- **The ledger.** The append-only record of what the loop and the supervisor
  did, for audit and for replay.
- **The tracker write path.** The single place that writes to the issue
  tracker, so tracker access is one seam and not scattered.

## What does not belong here

- **Any harness name.** Not in a module name, not in a function name, not in a
  string, not in a comment. If a harness name is needed, the code is in the
  wrong directory: it belongs in an adapter.
- Harness payload shapes, hook names, exit-code conventions, or transcript
  formats. An adapter translates those before core sees them.
- Prompt text. That lives in `prompts/`.
- Per-repo values such as a repository slug or a gate command. Those come from
  `loop.toml`, read through the configuration seam.
- Tracker vendor specifics beyond the one write path named above.

## Note

The code has not moved here yet. This directory describes the boundary that the
lift-out will have to respect.
