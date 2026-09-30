"""Read and validate one run state file for the supervisor.

The delivery loop's turn-end hook is the only writer of the state file; this
module is a read-only reader for the supervisor. The hook's own ``schema_ok()``
does not validate the fields the supervisor decides on (``status``, ``revision``,
timestamps, ``history``, guard maps), so this module validates every field it
exposes. Anything it needs but cannot trust makes the run ``unreadable`` or
``unsupported``, never an auto-answer input.

The state file lives in the worktree's git directory, not in the worktree, so
git never tracks it. ``state_path`` is the one definition of where it is, for
the hook that writes it and for this reader.

The reader never writes. It returns a ``(condition, state)`` pair where
``condition`` is one of ``CONDITIONS`` and ``state`` is the parsed dict when the
condition is ``ok``, else ``None``.

The stage list comes from the config, never from a constant here. The state file
records the list the run started under, and a file whose recorded list is not the
configured one reads as ``corrupt``. That is deliberate: it is what stops a run
started under one skillset being resumed under another.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
from datetime import datetime
from pathlib import Path

from core.config import DEFAULTS, LoopConfig

# The stage names of the default config, for a caller that reads this module
# with no config of its own. A configured loop reads ``stage_names(config)``.
STAGES = DEFAULTS.stage_names
STATUSES = ("running", "awaiting_human", "done", "failed")
CONDITIONS = ("ok", "missing", "corrupt", "unsupported", "unreadable")
SUPPORTED_VERSION = 1
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


# Variables that make git answer for another repository than the one ``-C``
# names, or configure it as the calling git did. A caller running inside a git
# hook exports them, so every git call here runs without them. The operator's
# own limits on repository search, such as GIT_CEILING_DIRECTORIES, are kept.
GIT_REPO_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_NAMESPACE",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
    }
)
# `git -c` exports numbered pairs, GIT_CONFIG_KEY_0 and GIT_CONFIG_VALUE_0 on.
_GIT_CONFIG_PAIR_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
# A `gitdir: <path>` pointer is one short line. Anything larger is not one.
_MAX_POINTER_BYTES = 4096


class NotAWorktree(ValueError):
    """A path git does not recognise as inside a worktree."""


def git_env() -> dict[str, str]:
    """The caller's environment without the variables that redirect git."""
    return {
        k: v
        for k, v in os.environ.items()
        if k not in GIT_REPO_VARS and not k.startswith(_GIT_CONFIG_PAIR_PREFIXES)
    }


def run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one git command in ``cwd`` without the redirecting variables.

    Raises ``OSError`` when git cannot be started at all.
    """
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )


def read_pointer(path: Path) -> str | None:
    """The text of a small regular file, or ``None``.

    git keeps each worktree link in a one-line file. The file is opened once,
    without following a symlink and without blocking on a pipe, and its type
    and size are checked on that same open file, so it cannot change between
    the check and the read.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        mode = os.fstat(fd)
        if not stat.S_ISREG(mode.st_mode) or mode.st_size > _MAX_POINTER_BYTES:
            return None
        data = os.read(fd, _MAX_POINTER_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(data) > _MAX_POINTER_BYTES:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _pointed_git_dir(worktree: Path) -> Path | None:
    """The git directory a worktree's ``.git`` entry names, read without git.

    ``.git`` is the directory itself in a main worktree and a one-line
    ``gitdir: <path>`` file in a linked one. Anything else returns ``None`` and
    the caller asks git instead.
    """
    dotgit = worktree / ".git"
    try:
        mode = dotgit.lstat()
    except OSError:
        return None
    if stat.S_ISDIR(mode.st_mode):
        return dotgit.resolve()
    text = read_pointer(dotgit)
    if text is None or not text.startswith("gitdir: "):
        return None
    target = Path(text.removeprefix("gitdir: ").strip())
    target = target if target.is_absolute() else worktree / target
    return target.resolve() if target.is_dir() else None


def git_dir(worktree: Path) -> Path:
    """The absolute git directory of one worktree.

    That is ``.git`` for the main worktree and ``.git/worktrees/<name>`` for a
    linked one. The worktree's own ``.git`` entry is read first, so a scan over
    many worktrees starts no process per worktree. A path below the worktree
    root, or an entry that cannot be read, is resolved by git.
    """
    pointed = _pointed_git_dir(worktree)
    if pointed is not None:
        return pointed
    try:
        result = run_git(worktree, "rev-parse", "--absolute-git-dir")
    except OSError as exc:
        raise NotAWorktree(f"cannot run git for {worktree}: {exc}") from exc
    if result.returncode != 0:
        raise NotAWorktree(f"{worktree} is not inside a git worktree: {result.stderr.strip()}")
    return Path(result.stdout.strip()).resolve()


def state_path_in(git_directory: Path, config: LoopConfig = DEFAULTS) -> Path:
    """Where the state file sits inside one worktree's git directory."""
    return git_directory / config.run_dir / config.state_file


def state_path(worktree: Path, config: LoopConfig = DEFAULTS) -> Path:
    """Where the hook keeps the state file for one worktree."""
    return state_path_in(git_dir(worktree), config)


def stage_names(config: LoopConfig = DEFAULTS) -> tuple[str, ...]:
    """The stage names this loop runs, in order."""
    return config.stage_names


def parse_iso(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp with an explicit offset, else ``None``."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def _int_ok(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _stages_ok(state: dict, config: LoopConfig) -> bool:
    names = config.stage_names
    cur = state.get("current")
    if state.get("stages") != list(names) or not _int_ok(cur):
        return False
    if not 0 <= cur < len(names) or state.get("current_stage") != names[cur]:
        return False
    return state.get("status") in STATUSES


def _counters_ok(state: dict, config: LoopConfig) -> bool:
    attempts = state.get("attempts")
    if not isinstance(attempts, dict) or set(attempts) != set(config.stage_names):
        return False
    if any(not _int_ok(v) for v in attempts.values()):
        return False
    return _int_ok(state.get("total_attempts")) and _int_ok(state.get("revision"))


def _identity_ok(state: dict) -> bool:
    if not UUID_RE.match(str(state.get("run_id", ""))):
        return False
    sid = state.get("session_id")
    if sid is not None and not isinstance(sid, str):
        return False
    return isinstance(state.get("history"), list)


def valid(state: object, config: LoopConfig = DEFAULTS) -> bool:
    """True when every field the supervisor reads is present and well typed.

    The recorded stage list must equal the configured one, so a state written
    under another skillset is not read as a run of this one.
    """
    if not isinstance(state, dict):
        return False
    if not (_stages_ok(state, config) and _counters_ok(state, config) and _identity_ok(state)):
        return False
    return parse_iso(state.get("updated_at")) is not None


def read_state(path: Path, config: LoopConfig = DEFAULTS) -> tuple[str, dict | None]:
    """Read and validate one state file. Never raises on a bad file."""
    if not path.exists():
        return ("missing", None)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ("unreadable", None)
    try:
        state = json.loads(raw)
    except (json.JSONDecodeError, ValueError, RecursionError):
        # A huge-int literal raises ValueError, not JSONDecodeError; deep nesting
        # raises RecursionError. Neither must crash the scan.
        return ("corrupt", None)
    if not isinstance(state, dict) or state.get("version") != SUPPORTED_VERSION:
        return ("unsupported", None)
    if not valid(state, config):
        return ("corrupt", None)
    return ("ok", state)
