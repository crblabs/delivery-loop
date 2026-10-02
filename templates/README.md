# templates

Files a host repository copies and fills in. A template is an example, never a
live configuration: nothing in this directory is read at run time.

## What belongs here

- `loop.toml`: the per-repo configuration seam. Every value the loop needs that
  differs from one host repository to the next, with a comment saying what it
  is and an empty value for the host to fill in.
- `zones.toml`: an example zone map for `loop-impact`. Copy it to
  `docs/architecture.zones.toml` in the host repository and replace the zones
  with your own. Without that file, `loop-impact` draws nothing and the ship
  stage leaves the `## Picture` section out of the pull request.
- `issue-audit.yml`: a GitHub Actions workflow that runs `loop-issue --audit`
  every Monday and fails when a P1 to P4 count rises above the baseline in
  `.issue-audit-baseline.json`. Copy it to `.github/workflows/`, set the
  `LINEAR_API_KEY` repository secret, then run `loop-issue --audit
  --update-baseline` once and commit the baseline it writes.
- Any further template a host repository would copy, if one is added later.

## What does not belong here

- A filled-in configuration for any real repository. Those live in the host
  repository, not here.
- Secrets, tokens or credentials of any kind.
- Code.

## Where the loop looks for the configuration

The loop reads the configuration from three places, in this order:

1. `loop.toml` at the root of the repository.
2. `~/.config/delivery-loop/<repo-slug>.toml` in your home directory.
3. The built-in defaults.

The repo slug is the name of the directory of the main checkout. All worktrees
of one repository use the same slug, so one user file configures all of them.
Two repositories whose main checkouts have the same directory name also share
one slug, and so share one user file.

If you do not give `--config`, a command reads the `loop.toml` at the root of
the repository that the current directory is in. It uses that configuration for
the state root and for a record that names no worktree. It judges each run under
the configuration of that run's own worktree. If you give `--config` with a file
or a directory, the command reads that file, or the `loop.toml` in that
directory, and applies it to every run.

The loop merges the files key by key. A key in the repository file wins over
the same key in the user file. A key that no file sets keeps its default. A
`[[stages]]` array replaces the whole stage list. The carve-out lists, and
`guard_watch`, add up across the files and the built-in lists. The built-in
carve-outs name `.claude/settings.json` and `.claude/settings.local.json`
literally, so moving `state_dir` cannot move them. So no file can remove a
carve-out. `loop.toml` is itself a built-in carve-out, so a run cannot edit it.

A run records the config it started under, in `config.json` in its run
directory, and the hooks judge the run by that copy. An edit to `loop.toml` or
the user file takes effect at the next `start`, not in a run already going.

A key that you write in the repository file overrides the user file, even when
it holds the default value. The template therefore ships `session_label` and
`[tracker]` commented out. Uncomment one only to share it with the whole team.

If the configuration of the current directory is present but is not valid, the
command writes a line to stderr and exits with code 2. The line starts with
`SUPERVISOR_CONFIG_INVALID` for `loop-scan`, `PRUNE_CONFIG_INVALID` for
`loop-prune`, and `CONFIG_INVALID` for the other commands. If only
the configuration of one run's worktree is not valid, the commands keep going:

- `loop-scan` reports the run with the condition `config_invalid`.
- `loop-decide` escalates the run as unobservable.
- `loop-pause-stats` counts the run as unobservable.
- `loop-card` shows the card with the wording of the current directory's
  configuration.

A run whose worktree is gone is orphaned. `loop-scan` marks it `orphaned`,
`loop-decide` escalates it with the reason `orphaned`, `loop-card` offers
`loop-prune --yes`, and `loop-pause-stats` counts it as unobservable.
`loop-prune` removes it. A record that is not marked orphaned, but whose
worktree disappeared after the scan, is treated as a run whose configuration is
not valid.

The loop asks git which repository a directory is in, and it ignores `GIT_DIR`
and similar variables from the environment. If git is missing, times out, or
refuses to read the repository, the command reports `CONFIG_INVALID`. It does
not continue without your user file, because that file can hold carve-outs.

A checkout whose directory name holds characters other than letters, digits,
`.`, `_` and `-` has no slug, and so no user file.

Because each run is read under its own worktree's configuration, a run is
checked against the stage list that its own branch declares, from whatever
checkout you run the commands.

Both files are optional. A repository with no `loop.toml` runs on the
defaults. A team can commit a `loop.toml` to share one configuration. Use the
user file for values that belong to you. The user file changes the
configuration of one repository only. For example:

```toml
# ~/.config/delivery-loop/my-repo.toml
[harness]
session_label = "Desk"
```

`state_root` and `state_file` say where every run's state lives. The hook and
every command must agree on that place, so a file sets them only when you name
it with `--config`. A `loop.toml` that the loop finds by itself, and the user
file, may not set them; the command reports `CONFIG_INVALID`. To move the state
for everything, set the `DELIVERY_LOOP_HOME` environment variable.

`.git` and everything under `.git/` are built-in carve-outs too, because they
name the repository and so the user file. Like every carve-out, they cover file
edits, not shell commands.

The loader also refuses these:

- A `loop.toml` at the repository root that is a symbolic link. Commit the file
  itself. A link that you name with `--config` is allowed.
- A `--config` that names no file.
- A tracker pattern that repeats a group that itself repeats, such as `(a+)+`,
  because matching it against a worktree name can take minutes.

## Note

`loop.toml` now holds two kinds of key. The ones `core/config.py` reads carry
the value the loop uses when the key is absent, and the template as it ships
reproduces the built-in defaults exactly. The ones marked "not read yet" are the shape the loop is
heading for: nothing reads them, and an unread key is ignored, not refused.
