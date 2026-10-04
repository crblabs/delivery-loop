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

## A time limit on a background wait (CRB-28 follow-up, with CRB-10)

- **What:** end or escalate a wait that has lasted past a configured limit,
  instead of relying on the 20-wait cap and the supervisor's `waiting_stale`.
- **Why:** a task that never reports keeps a run `running` until a person acts.
- **Pros:** an unattended run stops wasting a slot on a dead task.
- **Cons:** needs a timer outside the session; a limit that is too short cuts
  real long reviews.
- **Context:** CRB-28 adds `waiting_on`/`waiting_since` and the supervisor's
  `waiting_stale` escalation; CRB-10 is the place for the timer.
- **Effort:** M (human) / S (CC). **Priority:** P2.
- **Depends on:** CRB-28.

## Monitor and SendMessage-continued agents as pending tasks (CRB-28 follow-up)

- **What:** recognize a Monitor launch, and an agent woken again with
  SendMessage, as background work the Stop hook waits for.
- **Why:** today only Agent async launches, background Bash and their
  notifications are read; a Monitor wait is refused like a missing token.
- **Pros:** every harness wait tool works with the loop.
- **Cons:** no record shape observed yet; guessing risks phantom waits.
- **Context:** `adapters/claude_code/transcript_tasks.py`; capture the shapes
  from a real transcript first.
- **Effort:** S / S. **Priority:** P3.
- **Depends on:** CRB-28.

## Cache the transcript scan per session (CRB-28 follow-up)

- **What:** keep the last scanned byte offset and the launch/delivery id sets
  per transcript in the run directory, and scan only new bytes at each Stop.
- **Why:** the Stop hook reads the whole transcript every turn end; very long
  sessions make that slower.
- **Pros:** constant work per Stop.
- **Cons:** must notice a rewritten or truncated transcript and rescan.
- **Context:** `adapters/claude_code/transcript_tasks.py`. Measured: a 5 MB
  transcript scans in about 40 ms; the hard budget is `SCAN_BUDGET_S` (10 s).
- **Effort:** S / S. **Priority:** P3.
- **Depends on:** CRB-28.

## A background CI watch trips the 15 minute shell-only alarm (CRB-28 follow-up)

- **What:** give a background `gh run watch` or `gh pr checks --watch` the
  60 minute stale-wait limit instead of the 15 minute one for shell commands.
- **Why:** the wait guard tells the agent to run those watches in the
  background, which makes the wait shell-only; CI often runs past 15 minutes,
  so the supervisor escalates `waiting_stale` on a healthy run. Kept as is in
  the CRB-28 review (decision D10); found again by /qa on 2026-10-04.
- **Pros:** no false alarm on long CI.
- **Cons:** the scanner must read each background command's text from its
  launch's tool call.
- **Context:** `adapters/claude_code/transcript_tasks.py` (`Pending.shell`),
  `core/supervisor_scan.py` (`DEFAULT_SHELL_WAIT_STALE_S`).
- **Effort:** S / S. **Priority:** P3.
- **Depends on:** CRB-28.

## A megabyte-long shell word stalls the guard past its timeout

- **What:** make the shell lexer linear in a word's length, so a guarded shell
  call with a 1 MB word (a large `gh pr create --body`, a base64 argument) is
  read in well under a second.
- **Why:** shlex builds a word one character at a time, so its time grows with
  the square of the word: 1 MB takes about 40 s and the PreToolUse guard times
  out on that call. Found by /qa on 2026-10-04 (ISSUE-001), present on main.
- **Pros:** no timeout on large PR bodies or generated scripts.
- **Cons:** the first fix (shortening quoted spans) dropped text the guard
  reads and was reverted. A fix must keep every word whole: swap a long word for
  a placeholder before shlex and restore it after, mirroring shlex's quoting
  exactly, and send an unclosed quote straight to the per-line fallback.
- **Context:** `core/run_index.py` `_lex`; the regression cases are in the
  CRB-28 ship review (a slash-command resume in a long quoted prompt, a stray
  apostrophe on a long line, `$'...'` quoting).
- **Effort:** S / M. **Priority:** P2.
- **Depends on:** CRB-28.

## Background work started by a foreground subagent is not waited on (CRB-28 follow-up)

- **What:** wait on background tasks a foreground subagent launches, or treat
  a stage as unable to read its tasks when a subagent result shows it started
  background work.
- **Why:** the Stop hook reads only the main session transcript and skips
  sidechain entries, so a reviewer launched in the background by a subagent
  never counts as pending, and STAGE DONE can advance beside it. Found by the
  CRB-28 ship review on 2026-10-04; how the harness records nested launches is
  not yet checked.
- **Pros:** the wait covers every background reviewer, however it started.
- **Cons:** needs the nested launch's record shape, or subagent transcripts.
- **Context:** `adapters/claude_code/transcript_tasks.py` (`isSidechain` skip).
- **Effort:** S / M. **Priority:** P2.
- **Depends on:** CRB-28.
