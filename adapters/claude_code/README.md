# adapters/claude_code

The adapter for one harness: Claude Code.

## What belongs here

- The mapping from this harness's hook payloads to core events.
- The mapping from core verdicts back to what this harness expects: exit codes,
  response shapes, a blocked turn end, a reinjected message.
- The read of this harness's session identity.
- The read of this harness's pending question, where that is available. When it
  is not, this adapter reports the capability as absent and the loop degrades to
  state-file-only.
- Anything specific to this harness: hook names, payload field names, transcript
  layout, configuration file locations.

## What does not belong here

- Stage logic, pause reasons, guard rules, carve-out lists, ledger writes.
  Those are `core/`.
- Prompt text. That is `prompts/`.
- Per-repo values. Those are `loop.toml`.

## What is here

| Module | Does |
|---|---|
| `hooks.py` | Maps the `Stop`, `PreToolUse` and `UserPromptSubmit` payloads to `core.turn_end`, `core.guard` and `core.run_state`, and their verdicts back to Claude Code. A typed resume or abort is applied in `run_prompt`. |
| `bootstrap.py` | What the hook shims and the CLI do before anything needs the 3.11 floor: the no-run fast path, the resume and abort check on Bash and Skill calls, and the old-Python paths. Parses on Python 3.8. |
| `cli.py` | `delivery-loop`: start, status, resume, abort, doctor. |
| `supervisor_transcript.py` | Reads a session's pending question from its transcript (capability 4). |

The plugin's entry points sit at the repository root, where Claude Code looks
for them: `hooks/hooks.json` with its three shims, `commands/pipeline.md`,
`skills/pipeline/` and `bin/delivery-loop`. Which hook serves which capability
is recorded in `docs/adapter-contract.md`.
