# prompts

The stage prompts: the text the loop gives a coding agent at each stage.

## What belongs here

- One prompt per declared stage. A stage names its own file in the `prompt` key
  of its `[[stages]]` table in `loop.toml`, relative to this directory; a stage
  that names no file is given `<name>.md`. The default stages are autoplan,
  implement, qa, review and ship.
- Harness-neutral text. A prompt describes the work and the expected output. It
  does not name a harness.
- **A command name per stage.** Harnesses invoke their own commands under their
  own names. The `command` key of a stage holds the name the host harness
  actually uses, so the prompt text never spells one. A stage whose command is
  empty has no skill behind it: the model does the work directly.

## What does not belong here

- Harness-specific phrasing, slash-command syntax, or tool names written
  literally into prompt text. Route those through the stage's `command` key.
- Per-repo values such as a gate command or a repository slug. A prompt refers
  to the configuration key; `loop.toml` holds the value.
- Logic. A prompt is text. Decisions are `core/`.

## Note

The prompt text has not moved here yet.
