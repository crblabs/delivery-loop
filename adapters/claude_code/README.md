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

## Note

The adapter code has not moved here yet. Which of the four capabilities this
harness satisfies in full, and by which hooks, is recorded in
`docs/adapter-contract.md`. Parts of that mapping are unsettled.
