"""Read and validate one run state file for the supervisor.

The delivery loop's turn-end hook is the only writer of the state file; this
module is a read-only reader for the supervisor. The hook's own ``schema_ok()``
does not validate the fields the supervisor decides on (``status``, ``revision``,
timestamps, ``history``, guard maps), so this module validates every field it
exposes. Anything it needs but cannot trust makes the run ``unreadable`` or
``unsupported``, never an auto-answer input.

The reader never writes. It returns a ``(condition, state)`` pair where
``condition`` is one of ``CONDITIONS`` and ``state`` is the parsed dict when the
condition is ``ok``, else ``None``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

# Must stay equal to the stage list the writer uses, or a valid file reads
# as corrupt.
STAGES = ("autoplan", "implement", "qa", "review", "ship")
STATUSES = ("running", "awaiting_human", "done", "failed")
CONDITIONS = ("ok", "missing", "corrupt", "unsupported", "unreadable")
SUPPORTED_VERSION = 1
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def state_path(worktree: Path) -> Path:
    """Where the hook keeps the state file for one worktree."""
    return worktree / ".claude" / "pipeline.local.json"


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


def _stages_ok(state: dict) -> bool:
    cur = state.get("current")
    if state.get("stages") != list(STAGES) or not _int_ok(cur):
        return False
    if not 0 <= cur < len(STAGES) or state.get("current_stage") != STAGES[cur]:
        return False
    return state.get("status") in STATUSES


def _counters_ok(state: dict) -> bool:
    attempts = state.get("attempts")
    if not isinstance(attempts, dict) or set(attempts) != set(STAGES):
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


def valid(state: object) -> bool:
    """True when every field the supervisor reads is present and well typed."""
    if not isinstance(state, dict):
        return False
    if not (_stages_ok(state) and _counters_ok(state) and _identity_ok(state)):
        return False
    return parse_iso(state.get("updated_at")) is not None


def read_state(path: Path) -> tuple[str, dict | None]:
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
    if not valid(state):
        return ("corrupt", None)
    return ("ok", state)
