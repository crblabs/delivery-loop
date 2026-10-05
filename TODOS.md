# TODOS

Work considered while packaging the loop as a Claude Code plugin and left for
later, with why.

## Review findings left open at the /ship fix-cycle cap (P1, fix before or right after merge)

Found by the verification review of the third fix cycle (commit a9f4d44); /ship
stops fixing after three cycles, so they are listed here.

- **Bitbucket reads are judged as publishing (regression).** `kind_of` treats a
  tool as a read only when its first or last word is a read verb, so
  `mcp__bitbucket__bb_get_repo`, `bb_get_file`, `bb_ls_branches`,
  `bb_diff_branches` and `bb_get_commit_history` are denied outside the PR
  stage. Fix: drop a leading server prefix word (`bb`, `gl`) before the check,
  or accept a read verb anywhere before the first repository object; add `ls`
  to `_READ_VERBS`. File: `adapters/claude_code/bootstrap.py`.
- **Bitbucket's `bb_add_pr` is not seen as a write.** `pr`, `prs`, `mr`, `mrs`
  are not repository objects. Add them once the read fix above is in, so
  `bb_get_pr` stays a read.
- **Self-resume forms the early refusal misses.** `delivery-loop --sess abc
  resume` (argparse accepts the abbreviation), `eval "delivery-loop resume"`,
  `bash <<<"delivery-loop resume"`, `printf ... | sh`. The same abbreviation
  hides a start from the intent. Fix: `allow_abbrev=False` in
  `adapters/claude_code/cli.py`'s parser, and scan quoted words of `eval`,
  `sh`, `bash` segments. The CLI's terminal check still refuses both.
- **A heredoc commit message that mentions `delivery-loop resume` is refused.**
  `git commit -m "$(cat <<'EOF' ... EOF)"`: `_nested` scans the heredoc body as
  commands. Fix: drop heredoc bodies before scanning `$(...)` text.
  File: `core/run_index.py`.

## Guard what a shell command can reach outside the worktree

- **What:** hash the run's own state chain (a revision log) and the plugin cache
  at every turn end. (The user settings' switch keys are hashed already.)
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

## End a background wait past its limit (CRB-28 follow-up, with CRB-10)

- **What:** a timer outside the session that acts on a run whose wait passed
  its limit, rather than only escalating it.
- **Why:** CRB-28 escalates a stuck wait (`waiting_stale`, with the limits in
  `[supervisor]` of `loop.toml`), but nothing outside the session can end or
  release it; a person must resume or abort.
- **Pros:** an unattended run stops holding a slot on a dead task.
- **Cons:** the actor needs a person's authority (resume and abort are
  person-only); a limit too short cuts real long reviews.
- **Context:** CRB-10 is the place for the timer; CRB-28 provides
  `waiting_since`, per-task `task_since`, and the escalation.
- **Effort:** M (human) / S (CC). **Priority:** P2.
- **Depends on:** CRB-28, CRB-10.

## Known gaps from the CRB-28 ship review (CRB-28 follow-up)

- **What:** the last ship review's findings, shipped as known issues.
  - P1: a released agent that SendMessage resumes stays released, so STAGE DONE
    can advance beside it; give a resumed agent a new instance id.
  - P1: the first run's completion delivered after a SendMessage resume marks
    the resumed agent finished; count launches against deliveries per task.
  - P1: a Monitor whose event text or description contains `<status>...</status>`
    counts as finished; read the status from the notification header only.
  - P2: one shell word built from many short quoted pieces is still lexed in
    quadratic time (37 s at 2 MB); placeholder the whole word. A NUL byte in
    the line turns the placeholder pass off; pick a sentinel the line lacks.
  - P2: the scan cache in the run dir is trusted as written (edit it from the
    shell to hide a task), follows symlinks, and keeps no progress when a scan
    runs past its budget; check it against a digest in state.json and use the
    run dir's safe read and write helpers.
  - P3: a busy Monitor reaches the 20-wait limit while healthy; the scanner
    hardcodes Bash and run_in_background instead of the configured flag; the
    scan cache keeps commands that never launched; `--wait-stale-after-seconds 0`
    is ignored instead of refused.
- **Why:** each can let a stage advance beside running work or stall the guard;
  found by the CRB-28 ship review on 2026-10-05 and deferred to ship the branch.
- **Context:** `adapters/claude_code/transcript_tasks.py`, `core/run_index.py`,
  `core/turn_end.py`, `core/supervisor_scan.py`.
- **Effort:** M / S. **Priority:** P1.
- **Depends on:** CRB-28.
