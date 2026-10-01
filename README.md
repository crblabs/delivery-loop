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

The loop runs as a Claude Code plugin from this repository. The hooks, the
`/delivery-loop:pipeline` command, the stage skill and the stage prompts live
here, on top of the contracts in `core/`. A host repository needs no file of
its own.

Where a detail is not yet settled, this repository says "unsettled" rather than
guessing.

## Install

Once per machine, in Claude Code:

```
/plugin marketplace add crblabs/delivery-loop
/plugin install delivery-loop@crblabs
```

The same from a shell: `claude plugin marketplace add crblabs/delivery-loop`,
then `claude plugin install delivery-loop@crblabs --scope user`.

A user-scope install writes only to `~/.claude/settings.json` (the
`enabledPlugins` and `extraKnownMarketplaces` keys) and copies the plugin into
Claude Code's plugin cache. It adds nothing to any repository.

## Update

Auto-update is off for this marketplace, so an update is a step you take:

```
claude plugin update delivery-loop@crblabs
```

The plugin has no version number: every commit on `main` is a new version.
Update between runs, not during one. A run records the config it started under,
but a hook that changes under a running run may not read its state the same way.

## Prerequisites

- `python3` 3.11 or later first on `PATH`. The hooks and the CLI use the
  standard library only, and no package is installed. On an older `python3` the
  hooks do nothing where no run is active, and refuse to drive one that is:
  `delivery-loop abort` still works, so a run can always be ended.
- git, and a git repository to run in. A repository with no `origin` remote
  works: its runs are filed under `local/<directory name>`.
- The commands the default stages call: `/autoplan`, `/qa`, `/review` and
  `/ship`, from [gstack](https://github.com/garrytan/gstack). Or a `loop.toml`
  that declares other stages (see `templates/loop.toml`).
- macOS or Linux. Windows is not supported.

`delivery-loop doctor` checks each of these and says what to fix.

## Using it

Each verb has a slash form, which you type in Claude Code, and a shell form,
which the agent and a terminal use. The slash form runs the shell form with the
session's id, so the run is bound to the session that started it.

| Slash form | Shell form | Does |
|---|---|---|
| `/delivery-loop:pipeline start CRB-30` | `delivery-loop start CRB-30` | Starts a run in this worktree and hands the agent stage 1. |
| `/delivery-loop:pipeline status` | `delivery-loop status` | The run's stage, status, pause reason and last five events. |
| `/delivery-loop:pipeline resume` | `delivery-loop resume` | Continues a paused run. A person only: see below. |
| `/delivery-loop:pipeline abort` | `delivery-loop abort` | Ends the run. A person only: see below. |
| `/delivery-loop:pipeline doctor` | `delivery-loop doctor` | Checks python, git, the state home and the stage commands. |

The plugin puts `delivery-loop` on the agent's `PATH`. In a terminal of your
own, `delivery-loop doctor` (run once through the slash form) prints the full
path and an alias line.

`resume` and `abort` clear or end a pause, so only a person runs them. Typed in
Claude Code (`/delivery-loop:pipeline resume`, or `/pipeline resume` when no
other command has that name), the plugin's prompt hook applies them: Claude
Code runs that hook for what a person types and never for the agent's own tool
calls. The event log records such a resume as `by: person`. In a terminal, the
shell form works when both ends of the command are a terminal, and is recorded
as `by: terminal`. Anywhere else, `delivery-loop resume` and `abort` refuse.

When a run pauses, Claude Code shows you why, with the commands to continue or
end it. A gate's card, and `delivery-loop status`, show the plan file the run
read and the loop files it unlocks, so you approve what the run will act on.
`git push -u` writes the branch's upstream into the git config; that is not a
change the guard pauses for, but a change to a remote, a URL rewrite or a hook
path is.

A stage ends when the agent's last line is `<promise>STAGE DONE</promise>`, or
`<promise>NEEDS HUMAN</promise>` under a decision card when it needs you. A turn
that ends without either is sent back with the stage's instructions, and three
in a row pause the run. The `pipeline` skill in `skills/pipeline/SKILL.md` is the
agent's half of that contract.

## What the guard covers

During a run, before an edit tool writes a file:

- `loop.toml`, `.git`, `.claude/settings.json` and `.claude/settings.local.json`
  are carve-outs: an edit is always denied. Either settings file can switch the
  plugin's hooks off, so neither is ever editable from inside a run.
- A loop file, such as a stage prompt override under
  `.claude/skills/pipeline/stages/`, is editable only when the approved plan
  lists it in a ```` ```loop-edits ```` block.
- The plugin, `~/.claude` (or `$CLAUDE_CONFIG_DIR`), the loop's state home, and
  a `.claude/settings.json` or `settings.local.json` in any directory are
  denied, whatever the letter case.
- Every other file is ordinary work and is allowed.

The same rules apply to an MCP tool whose name says it writes (`write`, `edit`,
`create`, `move`, `delete` and the like) when it names a path. An MCP tool that
writes under another name is not checked.

A shell command is not checked before it runs, with two exceptions, and both
read the command text, so both are advisory. `git push` is allowed in the stage
that opens the pull request, for the run's own branch, to `origin`: never
forced, deleting or redirected with `-c`. And a shell line that runs
`delivery-loop resume` or `abort` is refused early with an explanation; the
CLI also refuses both outside a terminal. Neither is proof: an agent can give
a command a pseudo-terminal (`script`, `unbuffer`), spell the command so no
text check sees it, or start a nested `claude -p` session. Each of those is a
deliberate escape that leaves its trace in the session transcript, the same
class as rewriting the state file. The guard does not claim more.

A shell write to a guarded file is caught at the next turn end: every turn end
hashes the guarded files (the settings files by the keys that can switch hooks
off or repoint them: `disableAllHooks`, `enabledPlugins`, `hooks`, `env`), the
worktree's `.git` pointer and git config, and the run's config snapshot. A
change the plan did not declare pauses the run with `guard_changed`. What this
cannot catch: a shell write that switches the hooks off before that turn end, a
shell edit of the run's own state or of the plugin cache, and a shell command
that writes a resume into the state file itself. A determined agent with a
shell can get past every text-based check here; the guard makes that
deliberate and visible, not impossible. `loop-scan` flags a run whose index
entry disappeared (`index_missing`). The rest is in `TODOS.md`.

## Runbook

- **A run paused with `guard_changed`.** `delivery-loop status` lists the
  changed files. Check them. `/delivery-loop:pipeline resume` accepts them as
  the new baseline; `abort` ends the run.
- **A run paused with `no_message`.** The agent ended three turns without a
  token. Read the session, then resume or abort.
- **A run paused with `gate`.** A stage that asks for a person is done, the
  plan stage by default. Read its output, then resume to go on.
- **The hooks seem silent.** Run `/delivery-loop:pipeline doctor`, and check
  that `/plugin` lists delivery-loop as enabled. A run's events are in
  `events.jsonl` in its run directory (`delivery-loop status` prints the path).
- **The in-repo install is still there.** `start` refuses until it is gone. See
  the next section.
- **`python3` is too old.** Put 3.11 or later first on `PATH`, or end the run
  with `delivery-loop abort`, which works on any `python3`.

## Moving from the in-repo install

A repository that ran the loop from its own `.claude/` directory:

1. Finish or abort the runs the old hooks drive. The plugin's hooks leave those
   runs alone: they only see runs the plugin started.
2. Delete `.claude/hooks/pipeline_*`, `.claude/skills/pipeline/` (keep a stage
   prompt you changed: under `.claude/skills/pipeline/stages/` it still
   overrides the plugin's), and the loop's hook entries in
   `.claude/settings.json`.
3. In `loop.toml`, change `/pipeline resume` and `/pipeline abort` to
   `/delivery-loop:pipeline resume` and `abort`, or delete the two keys.
4. Install the plugin and start a run.

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

## Commands

The supervisor's tools ship as console entry points, so a runbook calls them by
name and not by file path. They are operator tools: install them from a checkout
of this repository. The plugin does not ship them. A path can move; a name is stable. `uv sync` installs them into
the project environment, and `uv run <name>` runs one. The names share the
`loop-` prefix so they group together in a shell.

| Command | Does |
|---|---|
| `loop-scan` | Finds every run under the state root and prints one JSON record each. |
| `loop-decide` | Reads one scan record and prints the decision for that run. |
| `loop-card` | Renders one run's pause as the decision card a person reads. |
| `loop-card-check` | Checks that a pause question carries the required card shape. |
| `loop-pause-stats` | Counts the pauses in a scan by category. |
| `loop-prune` | Lists the runs whose worktree is gone, and deletes them and their index entries with `--yes`. |
| `loop-transcript` | Reads the pending question out of one harness session. |

Every run's state lives outside the repository, under `~/.delivery-loop`, in
`runs/<owner>-<repo>/<worktree>-<hash>/`, and the run index the hooks find a run
by lives beside it, in `index/`. A host repository needs no ignore
rule. Set `DELIVERY_LOOP_HOME`, or `state_root` in a `loop.toml` that you name
with `--config`, to move it. A `loop.toml` that a command finds by itself may not
move it, so the hook and every command agree. See `templates/README.md`.

Each command takes `--help`. `[project.scripts]` in `pyproject.toml` is the
table that declares them, and `tests/test_entry_points.py` reads that table and
fails if a name does not resolve to a callable `main`.

## Layout

| Directory | Holds |
|---|---|
| `core/` | The stage machine and everything harness-neutral. |
| `adapters/claude_code/` | The adapter for one harness: its hooks, CLI and transcript reader. |
| `.claude-plugin/` | The plugin manifest and the `crblabs` marketplace that lists it. |
| `hooks/` | The plugin's hook declarations and the two hook entry points. |
| `commands/` | The `/delivery-loop:pipeline` slash command. |
| `skills/pipeline/` | The stage skill and the default stage prompts. |
| `bin/` | `delivery-loop`, which the plugin puts on the agent's `PATH`. |
| `templates/` | `loop.toml`, the per-repo configuration a host repo fills in. |
| `docs/` | The contract and the operator runbooks. |

Each directory carries a `README.md` or a docstring saying what belongs in it
and what does not.

## Developing this repository

`uv sync` installs the dev tools; `uv run pytest -q`, `uv run ruff check .` and
`uv run ruff format --check .` are the checks CI runs, on Python 3.11, 3.12 and
3.13.

To try the plugin from a checkout without installing it, start Claude Code with
`claude --plugin-dir .`. A local marketplace works too:
`claude plugin marketplace add ./`.

The repository root is the plugin root, and Claude Code loads a `.mcp.json`
from a plugin root for every user, so this repository has none. To use the
Linear server while working on it, add it to your local scope:

```
claude mcp add --scope local --transport http linear-crblabs https://mcp.linear.app/mcp \
  --header "Authorization: Bearer ${LINEAR_API_KEY}"
```
