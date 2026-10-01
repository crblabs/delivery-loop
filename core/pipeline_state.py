"""Read and validate one run state file for the supervisor.

The delivery loop's turn-end hook is the only writer of the state file; this
module is a read-only reader for the supervisor. The hook's own ``schema_ok()``
does not validate the fields the supervisor decides on (``status``, ``revision``,
timestamps, ``history``, guard maps), so this module validates every field it
exposes. Anything it needs but cannot trust makes the run ``unreadable`` or
``unsupported``, never an auto-answer input.

The state file does not live in the worktree, so git never sees it and a host
repository needs no ignore rule. Each run has a directory under the state root,
``~/.delivery-loop`` unless ``$DELIVERY_LOOP_HOME`` or the config moves it:
``runs/<owner>-<repo>/<worktree>-<hash>/``. ``state_path`` is the one definition
of where the state file is, for the hook that writes it and for this reader.
``prepare_run_dir`` is the hook's one helper here and the only function that
writes.

The reader never writes. It returns a ``(condition, state)`` pair where
``condition`` is one of ``CONDITIONS`` and ``state`` is the parsed dict when the
condition is ``ok``, else ``None``.

The stage list comes from the config, never from a constant here. The state file
records the list the run started under, and a file whose recorded list is not the
configured one reads as ``corrupt``. That is deliberate: it is what stops a run
started under one skillset being resumed under another.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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


# The variable that moves the state root, as GSTACK_HOME moves gstack's.
HOME_ENV = "DELIVERY_LOOP_HOME"
RUNS_DIR = "runs"
# The file in a run directory that names the worktree the run belongs to.
WORKTREE_FILE = "worktree"
# The worktree file holds one path. Anything larger is not one.
_MAX_WORKTREE_BYTES = 4096
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def state_home(config: LoopConfig = DEFAULTS) -> Path:
    """The root every run lives under: ``$DELIVERY_LOOP_HOME``, else the config's."""
    return Path(os.environ.get(HOME_ENV) or config.state_root).expanduser()


def _name(text: str) -> str:
    """One safe path component made from a free-form name."""
    return _UNSAFE_NAME_RE.sub("-", text).strip(".-") or "run"


def repo_slug(repo: str) -> str:
    """The directory name of one repository: ``owner/name`` becomes ``owner-name``."""
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError(f"a run needs its repository as owner/name, got {repo!r}")
    return _name(repo.strip().replace("/", "-"))


def run_dir(worktree: Path, repo: str, config: LoopConfig = DEFAULTS) -> Path:
    """The directory that holds one worktree's run.

    It is named after the worktree's folder plus a short hash of the worktree's
    real path, so two worktrees with the same folder name never share one.
    """
    real = worktree.resolve()
    digest = hashlib.sha256(str(real).encode("utf-8")).hexdigest()[:12]
    return state_home(config) / RUNS_DIR / repo_slug(repo) / f"{_name(real.name)}-{digest}"


def state_path(worktree: Path, repo: str, config: LoopConfig = DEFAULTS) -> Path:
    """Where the hook keeps the state file for one worktree."""
    return run_dir(worktree, repo, config) / config.state_file


def prepare_run_dir(worktree: Path, repo: str, config: LoopConfig = DEFAULTS) -> Path:
    """Create one worktree's run directory and return its state file path.

    The hook calls this before its first write. It records which worktree the
    run belongs to, so the supervisor can map the run back to it and notice
    when the worktree is gone.
    """
    directory = run_dir(worktree, repo, config)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / WORKTREE_FILE).write_text(f"{worktree.resolve()}\n", encoding="utf-8")
    return directory / config.state_file


def _read_small_file(path: Path) -> str | None:
    """The text of a small regular file, or ``None``.

    The file is opened once, without following a symlink and without blocking
    on a pipe, and its type and size are checked on that open file.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        mode = os.fstat(fd)
        if not stat.S_ISREG(mode.st_mode) or mode.st_size > _MAX_WORKTREE_BYTES:
            return None
        data = os.read(fd, _MAX_WORKTREE_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return None if len(data) > _MAX_WORKTREE_BYTES else data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def read_worktree(directory: Path) -> Path | None:
    """The worktree one run directory belongs to, or ``None`` when unrecorded."""
    text = _read_small_file(directory / WORKTREE_FILE)
    value = "" if text is None else text.strip()
    if not value or "\n" in value or not os.path.isabs(value):
        return None
    return Path(value)


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
