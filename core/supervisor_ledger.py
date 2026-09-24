#!/usr/bin/env python3
"""The supervisor's private ledger and single-supervisor lock.

The ledger is dedup and re-escalation memory, home-scoped and namespaced per
repository at ``~/.claude/supervisor/<repo-slug>-ledger.local.jsonl``. It is not
a run's state file; the supervisor owns it. Notifications are at-least-once, not
exactly-once: a crash after an external send but before the ledger append
re-notifies on restart.

Two supervisors on one repository would both read "not notified", both send, and
both append, which no atomic append can fix, so a ``flock`` lock refuses a second
supervisor per repository. The kernel frees the lock when the holder dies.

Pause identity is run-scoped. ``failed`` and ``done`` carry their own terminal
identities so a pause notification never suppresses a later failure, and ``done``
is never re-notified. A ``permission_prompt`` question carries its own identity,
keyed on the ``tool_use_id`` before the terminal check, so a prompt on a done run
still notifies rather than collapsing into that run's ``done`` key.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from datetime import datetime
from pathlib import Path

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def ledger_path(repo_slug: str, home: Path | None = None) -> Path:
    """Where this repository's ledger lives.

    The slug names one file, never a path: a slug with a separator or ``..``
    would let the ledger escape the supervisor directory, so it is refused.
    """
    if not _SLUG_RE.match(repo_slug) or repo_slug in (".", ".."):
        raise ValueError(f"unsafe repo slug: {repo_slug!r}")
    base = (home or Path.home()) / ".claude" / "supervisor"
    return base / f"{repo_slug}-ledger.local.jsonl"


def acquire_lock(repo_slug: str, home: Path | None = None):
    """Take the single-supervisor lock, or return ``None`` if another holds it."""
    path = ledger_path(repo_slug, home).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_NOFOLLOW refuses to open the lock through a symlink, so a planted symlink
    # cannot redirect the truncate below onto a run's state file.
    try:
        handle = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        return None
    os.ftruncate(handle, 0)
    os.write(handle, str(os.getpid()).encode())
    return handle


def pause_key(record: dict, question: dict | None = None) -> str:
    """A stable, run-scoped identity for the pause or terminal state."""
    # An unobservable run has no run_id, so the worktree disambiguates it; two
    # corrupt runs must not share one dedup key and suppress each other.
    run = record.get("run_id") or record.get("worktree") or "?"
    status = record.get("status")
    if isinstance(question, dict) and question.get("outcome") == "permission_prompt":
        # Checked before the terminal status, so a prompt on a done run gets its
        # own identity and is not suppressed by the run's already-sent ":done".
        return f"{run}:permprompt:{question.get('tool_use_id')}"
    if status in ("failed", "done"):
        return f"{run}:{status}"
    if status == "awaiting_human":
        return f"{run}:pause:{record.get('paused_reason')}:{record.get('paused_prompt_id')}"
    if isinstance(question, dict) and question.get("outcome") == "pending":
        return f"{run}:gate:{question.get('tool_use_id')}"
    return f"{run}:{status or record.get('condition')}"


def read_ledger(path: Path) -> list[dict]:
    """Every ledger entry; a truncated or unreadable file is tolerated, not fatal."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    entries = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def _last_ts(entries: list[dict], key: str, channel: str) -> datetime | None:
    times = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("key") == key and entry.get("channel") == channel:
            dt = _parse(entry.get("ts"))
            if dt is not None:
                times.append(dt)
    return max(times) if times else None


def _parse(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None


def should_notify(
    entries: list[dict], key: str, channel: str, now: datetime, reescalate_after: int
) -> bool:
    """True when this channel has never notified this key, or long enough ago."""
    last = _last_ts(entries, key, channel)
    if last is None:
        return True
    if key.endswith(":done"):
        return False
    return (now - last).total_seconds() >= reescalate_after


def _needs_newline_prefix(fd: int) -> bool:
    if os.fstat(fd).st_size == 0:
        return False
    os.lseek(fd, -1, os.SEEK_END)
    return os.read(fd, 1) != b"\n"


def append_notification(path: Path, key: str, channel: str, now: datetime) -> None:
    """Record one delivered notification as a single atomic line append.

    A torn final line from an earlier crash would otherwise merge with this one
    and lose both, so this heals a missing trailing newline before it appends.
    ``O_NOFOLLOW`` refuses to append through a symlink, so a planted ledger
    symlink cannot redirect the write into a run's state file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"key": key, "channel": channel, "ts": now.isoformat()}, sort_keys=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND | os.O_NOFOLLOW, 0o644)
    try:
        prefix = b"\n" if _needs_newline_prefix(fd) else b""
        os.write(fd, prefix + payload.encode() + b"\n")
    finally:
        os.close(fd)
