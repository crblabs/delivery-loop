# TODOS

Work considered while packaging the loop as a Claude Code plugin and left for
later, with why.

## Guard what a shell command can reach outside the worktree

- **What:** hash the run's own state chain (a revision log), the plugin cache and
  the plugin keys of `~/.claude/settings.json` at every turn end.
- **Why:** the edit guard denies edit tools there, but a shell command is not
  checked before it runs, so it can rewrite the state file or the cached hooks.
- **Context:** `core/guard_evidence.py` builds the guard map from worktree paths
  and the run's config snapshot. A revision chain would let the supervisor tell
  a hook's write from any other.
- **Effort:** M (human) / S (CC).

## Notice hooks that went silent

- **What:** a supervisor check that flags a running run whose session
  transcript keeps growing while its `hook_seen` and `events.jsonl` do not.
- **Why:** a shell write that sets `disableAllHooks` stops the hooks before the
  turn end that would have caught it.
- **Context:** `core/supervisor_scan.py` already reads `hook_seen`; the
  transcript reader is `adapters/claude_code/supervisor_transcript.py`.
- **Effort:** M / S.

## Default stages that need no other skill suite

- **What:** a stage list that runs on this plugin alone, chosen when the
  `/autoplan`, `/qa`, `/review` and `/ship` commands are not installed.
- **Why:** a fresh machine without gstack starts a run that stops at stage 1.
  `delivery-loop doctor` warns, but does not fix it.
- **Effort:** M / S.

## Ship the supervisor through the plugin

- **What:** expose `loop-scan`, `loop-decide`, `loop-card` and `loop-prune` as
  `delivery-loop` subcommands, or as files in `bin/`.
- **Why:** an operator installs them from a checkout today.
- **Effort:** S / S.

## Versioned releases

- **What:** a `version` in `.claude-plugin/plugin.json`, bumped by the ship
  stage, or a marketplace entry pinned to a release tag.
- **Why:** with no version every commit is a release, so a user cannot pin or
  roll back to a known one.
- **Context:** the plugin ships without a version on purpose, so every commit
  reaches users who update.

## A SessionStart status line

- **What:** a `SessionStart` hook that prints the active run's stage and the
  resume command when a session opens in a run's worktree.
- **Why:** a person returning to a paused run sees it without asking.
- **Context:** every session on the machine would pay for it; `status` covers
  the need today.

## Plugin validation in CI

- **What:** run `claude plugin validate .` in CI.
- **Why:** `tests/test_plugin_manifest.py` checks the shape this repository
  relies on, not everything Claude Code checks.
- **Context:** needs the Claude Code CLI on the runner.

## Evals for the stage prompts

- **What:** an eval suite that runs each stage prompt against a fixture task.
- **Why:** a prompt change is only checked by a real run today.

## Two small inconsistencies in hook notes (found by /qa, deferred as low)

- **What:** the gate's pause note repeats "Continue / End" after a card whose
  REC and ALT lines already name the same two commands; and on a python3 older
  than the floor, a typed abort's note lacks the `delivery-loop abort:` prefix
  the normal path uses.
- **Why:** cosmetic; the person still sees the right commands and result.
- **Repro:** end a gated stage (`PLAN: <file>` then the done token) and read the
  Stop hook's `systemMessage`; type `/delivery-loop:pipeline abort` with a
  python3 3.10 or older first on PATH.
- **Context:** `adapters/claude_code/hooks.py` `_pause_note`;
  `adapters/claude_code/bootstrap.py` `old_python_hook`.
