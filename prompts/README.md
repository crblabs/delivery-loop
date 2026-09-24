# prompts

The stage prompts: the text the loop gives a coding agent at each of the five
stages.

## What belongs here

- One prompt per stage: autoplan, implement, qa, review, ship.
- Harness-neutral text. A prompt describes the work and the expected output. It
  does not name a harness.
- **A command-name map.** Harnesses invoke their own commands under their own
  names. A prompt refers to a command by a neutral key, and the map turns that
  key into the name the host harness actually uses. The map for a given host
  repository is filled in under `[commands]` in `loop.toml`.

## What does not belong here

- Harness-specific phrasing, slash-command syntax, or tool names written
  literally into prompt text. Route those through the command-name map.
- Per-repo values such as a gate command or a repository slug. A prompt refers
  to the configuration key; `loop.toml` holds the value.
- Logic. A prompt is text. Decisions are `core/`.

## Note

The prompt text has not moved here yet. The exact set of keys in the
command-name map is unsettled.
