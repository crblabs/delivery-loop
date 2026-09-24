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
- **The configuration seam.** `config.py`: one frozen `LoopConfig` holding
  every value that names the harness or the tracker, and the loader that reads
  a `loop.toml` over it.

## How a value gets into the config, and not into a literal

Every value that names the harness or the tracker is a field on `LoopConfig` in
`config.py`, and nowhere else. To add one:

1. Add the field to `LoopConfig` with today's value as its default. Write a
   path under the harness state directory with the `{state_dir}`,
   `{state_file}` or `{state_stem}` placeholder, so one setting moves every
   path that names it. A tracker value takes `{tracker_prefix}` the same way.
2. Add its table and key to `_STRING_KEYS` or `_LIST_KEYS`, so a `loop.toml`
   can set it, and validate it in `_validate` if a wrong value could do damage.
3. Take the config as a parameter where the value is used. A public function
   gives that parameter the default config, so every existing caller keeps
   working; a private one takes it positionally, from its public caller.
4. Write the key into `templates/loop.toml` with the same default and a comment
   saying what it is.

The config is built once, at the entry point, from a `loop.toml` when the
operator names one, and is passed down from there. No module reads a file, an
environment variable or a mutable global at import time. A missing, empty or
partial `loop.toml` yields the defaults, so a host that has configured nothing
gets exactly the behaviour the loop had when the values were literals.

## What does not belong here

- **Any harness name, outside `config.py`.** Not in a module name, not in a
  function name, not in a string, not in a comment. `config.py` is the one
  place a harness default may be spelled, because writing it there is what
  makes it replaceable; everywhere else the value comes from the config. If a
  harness name is needed in logic, the code is in the wrong directory: it
  belongs in an adapter.
- Harness payload shapes, hook names, exit-code conventions, or transcript
  formats. An adapter translates those before core sees them.
- Prompt text. That lives in `prompts/`.
- Per-repo values such as a repository slug or a gate command. Those come from
  `loop.toml`, read through the configuration seam.
- Tracker vendor specifics beyond the one write path named above.

## Note

The code has not moved here yet. This directory describes the boundary that the
lift-out will have to respect.
