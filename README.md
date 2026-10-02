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
`delivery-loop start` refuses to start a run while a stage's command is not
found in your skills, the repository's `.claude/skills` or an installed plugin.

## Using it

Each verb has a slash form, which you type in Claude Code, and a shell form,
which the agent and a terminal use. The slash form runs the shell form with the
session's id, so the run is bound to the session that started it.

| Slash form | Shell form | Does |
|---|---|---|
| `/delivery-loop:pipeline start <task>` | `delivery-loop start <task>` | Starts a run in this worktree and hands the agent stage 1. The task is an issue id or a short title. |
| `/delivery-loop:pipeline status` | `delivery-loop status` | The run's stage, status, pause reason and last five events. |
| `/delivery-loop:pipeline resume` | `delivery-loop resume` | Continues a paused run. A person only: see below. |
| `/delivery-loop:pipeline abort` | `delivery-loop abort` | Ends the run. A person only: see below. |
| `/delivery-loop:pipeline doctor` | `delivery-loop doctor` | Checks python, git, the state home and the stage commands. |

The plugin puts `delivery-loop` on the agent's `PATH`. In a terminal of your
own, `delivery-loop doctor` (run once through the slash form) prints the full
path and an alias line.

`resume` and `abort` clear or end a pause, so only a person runs them. Typed in
Claude Code in full (`/delivery-loop:pipeline resume`; a short `/pipeline
resume` is not applied), the plugin's prompt hook applies them: Claude Code
runs that hook for what a person types and never for the agent's own tool
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
- The plugin, `~/.claude` (or `$CLAUDE_CONFIG_DIR`), the loop's state home,
  your loop config in `~/.config/delivery-loop/`, your git config
  (`~/.gitconfig`, `~/.config/git/config`), and a `.claude/settings.json` or
  `settings.local.json` in any directory are denied, whatever the letter case.
- Every other file is ordinary work and is allowed.

The same rules apply to an MCP tool whose name says it writes (`write`, `edit`,
`create`, `move`, `delete` and the like) to every path it names, a move's
source as well as its destination. A code host's MCP
tool that writes to the remote (a GitHub, GitLab, Bitbucket or Gitea server's
tools that name files, branches, commits, releases or pull requests, other
than its `get`, `list`, `search` and `read` tools) is allowed only in the stage
that opens the pull request, only for the run's own branch, and a `merge` tool
never.
An MCP tool that writes under another name is not checked.

A shell command is not checked before it runs, with three exceptions, and all
read the command text, so all are advisory. `git push` is allowed in the stage
that opens the pull request, for the run's own branch, to `origin`: never
forced, deleting or redirected with `-c`. `gh pr merge` and a merge through
`gh api` are refused: a merge is a person's call. Other `gh` commands that
write to the remote are not checked. And a shell line that runs
`delivery-loop resume` or `abort`, directly, behind a wrapper or in a nested
shell, is refused early with an explanation; a line that only mentions them,
such as a commit message, is not. The CLI also refuses both outside a terminal,
and only the full `/delivery-loop:pipeline resume|abort`, typed by a person,
applies them from Claude Code. Neither is proof: an agent can give
a command a pseudo-terminal (`script`, `unbuffer`), spell the command so no
text check sees it, or start a nested `claude -p` session. Each of those is a
deliberate escape that leaves its trace in the session transcript, the same
class as rewriting the state file. The guard does not claim more.

A shell write to a guarded file is caught at the next turn end: every turn end
hashes the guarded files (the settings files by the keys that can switch hooks
off or repoint them: `disableAllHooks`, `enabledPlugins`, `hooks`, `env`), your
user settings by the same keys (only delivery-loop's own `enabledPlugins`
entry), the worktree's `.git` pointer, the git config a push reads (the
repository's, the worktree's `config.worktree`, yours, and the files their
`include.path` names) and the run's config snapshot. A change the plan did not
declare pauses the run with `guard_changed`; a carve-out is never accepted,
even under a directory the plan declared. What this
cannot catch: a shell write that switches the hooks off before that turn end, a
shell edit of the run's own state or of the plugin cache, and a shell command
that writes a resume into the state file itself. A determined agent with a
shell can get past every text-based check here; the guard makes that
deliberate and visible, not impossible. `loop-scan` flags a run whose index
entry disappeared (`index_missing`).

The hooks answer before Claude Code's own timeout: a guard check that runs
past 25 seconds denies the call, and a turn end past 55 seconds keeps the turn
going once. A hook that cannot start at all, such as when `python3` is missing
from `PATH`, is a non-blocking error in Claude Code and lets the call through;
`/delivery-loop:pipeline doctor` checks the `python3` the hooks run with. The
rest is in `TODOS.md`.

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
- **The run stopped being driven after `/clear` or in a new session.** The run
  stays bound to the session that started it; the new session is told once.
  Type `/delivery-loop:pipeline resume` there to drive it from that session.
- **The loop is in the way and you need it gone now.** Type
  `/delivery-loop:pipeline abort` to end the run, then `/plugin disable
  delivery-loop` to turn the hooks off in every session. `/plugin enable
  delivery-loop` brings them back.

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
name and not by file path. A path can move; a name is stable. The plugin puts
the ones the supervisor skill calls (`loop-scan`, `loop-transcript`,
`loop-decide`, `loop-card`, `loop-ledger`, `loop-pause-stats` and `loop-prune`) in `bin/`, on
the agent's `PATH`, so a session runs them by name. From a checkout of this
repository, `uv sync` installs them all into the project environment, and
`uv run <name>` runs one. The names share the `loop-` prefix so they group
together in a shell. The `supervisor` skill in `skills/supervisor/` is the
routine that drives them: start it with `/loop /delivery-loop:supervisor` in a
checkout with no active run.

| Command | Does |
|---|---|
| `loop-scan` | Finds every run under the state root and prints one JSON record each. |
| `loop-decide` | Reads one scan record and prints the decision for that run. |
| `loop-card` | Renders one run's pause as the decision card a person reads. |
| `loop-card-check` | Checks that a pause question carries the required card shape. |
| `loop-ledger` | The supervisor's lock and notification ledger: `lock` and `unlock` one supervisor per repository, `check` whether a pause was already notified, `record` a notification. |
| `loop-pause-stats` | Counts the pauses in a scan by category. |
| `loop-prune` | Lists the runs whose worktree is gone, and deletes them and their index entries with `--yes`. |
| `loop-transcript` | Reads the pending question out of one harness session. |
| `loop-no-dash` | Fails when a line the diff adds, a commit message or a PR body holds an en dash, an em dash, a figure dash or a horizontal bar. Exceptions and excluded directories live in `.dash-exceptions.json` in the host repository. The plugin also ships it in `bin/`, and the implement, qa, review and ship stages run it. |
| `loop-comments` | Fails when a line the diff adds to a Python file breaks comment hygiene: a comment block or a private docstring over three lines, a ticket or PR id in a comment or docstring, commented-out code (when `ruff` is on `PATH`), or more than one comment line per four code lines. Which files it reads and which ids it bans live in `.comments-policy.json` in the host repository. The plugin also ships it in `bin/`. |
| `loop-complexity` | Fails when a function the diff adds or changes is over the cyclomatic bar (10) or the length bar (60 statements, tests exempt), or grew past its recorded number. A ratchet: existing code is listed, with its numbers, in `.complexity-baseline.json` in the host repository, and that list may only shrink. The file also holds the bars, the directories to scan and which files are tests. The plugin also ships it in `bin/`. |
| `loop-pr-body` | Fails when a pull request body lacks a required level-2 section. The host repository names the sections in `.pr-sections` at its root, one per line, or with `--sections`. With neither, it passes. A heading inside a code block or an HTML comment does not count. The plugin also ships it in `bin/`, and the ship stage runs it. |
| `loop-pr-decisions` | Reads a JSON array of decisions on stdin and prints the pull request's `## Decisions` section, one line per decision, or `None`. The plugin also ships it in `bin/`, and the ship stage runs it. |
| `loop-impact` | Prints the impact map of a branch: which zones of the host's zone map the diff touched, directly or through a declared edge or a Python import, as a mermaid flowchart and a table. The map lives in `docs/architecture.zones.toml` in the host repository, or `--map`; `templates/zones.toml` is an example to copy. With no map it prints one line and exits 0. It fails when a changed file is owned by no zone. The plugin also ships it in `bin/`, and the ship stage runs it. |
| `loop-pre-push` | The git pre-push hook. It runs the rules the Stop hook enforces on every pushed branch: `loop-no-dash` on the added lines and the pushed commit messages, and `loop-comments` and `loop-complexity` unless `[checks]` turns them off. Install it as `.git/hooks/pre-push` with `exec loop-pre-push "$@"`, after `uv tool install` of this repository puts the commands on your `PATH`. |

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
| `hooks/` | The plugin's hook declarations and their one entry point, `pipeline_hook.py`. |
| `commands/` | The `/delivery-loop:pipeline` slash command. |
| `skills/pipeline/` | The stage skill and the default stage prompts. |
| `skills/supervisor/` | The supervisor skill and the routine it follows. |
| `bin/` | `delivery-loop`, the check commands (`loop-no-dash`, `loop-comments`, `loop-complexity`, `loop-pr-body`, `loop-pr-decisions`, `loop-pre-push`, `loop-impact`) and the supervisor's `loop-*` commands, which the plugin puts on the agent's `PATH`. |
| `templates/` | `loop.toml`, the per-repo configuration a host repo fills in, and `zones.toml`, an example zone map for `loop-impact`. |
| `docs/` | The contract and the operator runbooks. |
| `scripts/` | Developer checks: the steps CI runs and the edit-time ruff hook. |

Each directory carries a `README.md` or a docstring saying what belongs in it
and what does not.

## Developing this repository

`uv sync` installs the dev tools. `scripts/check.sh` runs the checks CI runs
(tests, lint, format and the house rules), the same script CI calls on Python
3.11, 3.12 and 3.13; name steps to run only those (`scripts/check.sh lint
format`).

To run it before every push, install the hook once per clone:

```
git config core.hooksPath .githooks
```

If you already have a pre-push hook, call `.githooks/pre-push` from it instead.
A Claude Code session in this repository also formats and lints each Python
file an edit tool writes (`.claude/settings.json`, `scripts/ruff-on-edit.sh`),
so ruff's findings come back in the same turn.

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
