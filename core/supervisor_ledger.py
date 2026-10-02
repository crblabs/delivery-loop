#!/usr/bin/env python3
"""The supervisor's private ledger and single-supervisor lock.

The ledger is dedup and re-escalation memory, home-scoped and namespaced per
repository: one file per repository slug, in the directory the config names
under the operator's home. It is not a run's state file; the supervisor owns it.
Notifications are at-least-once, not exactly-once: a crash after an external
send but before the ledger append re-notifies on restart.

Two supervisors on one repository would both read "not notified", both send, and
both append, which no atomic append can fix, so a ``flock`` lock refuses a second
supervisor per repository. The kernel frees the lock when the holder dies.

Pause identity is run-scoped. ``failed`` and ``done`` carry their own terminal
identities so a pause notification never suppresses a later failure, and ``done``
is never re-notified. A ``permission_prompt`` question carries its own identity,
keyed on the ``tool_use_id`` before the terminal check, so a prompt on a done run
still notifies rather than collapsing into that run's ``done`` key.

``loop-ledger`` exposes these functions to the supervisor routine. ``lock`` takes
the lock and holds it until the process is ended, and ``unlock`` ends that
process. ``check`` exits 0 when a pause should be notified on a channel and 1
when it should not. ``record`` appends one delivered notification. ``path``
prints where the ledger lives.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import sys
from datetime import datetime
from pathlib import Path

from core import pipeline_state as ps
from core.config import DEFAULTS, LoopConfig, load_cli_config

_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def ledger_path(repo_slug: str, home: Path | None = None, config: LoopConfig = DEFAULTS) -> Path:
    """Where this repository's ledger lives.

    The slug names one file, never a path: a slug with a separator or ``..``
    would let the ledger escape the supervisor directory, so it is refused.
    """
    if not _SLUG_RE.match(repo_slug) or repo_slug in (".", ".."):
        raise ValueError(f"unsafe repo slug: {repo_slug!r}")
    return (home or Path.home()) / config.ledger_dir / config.ledger_file(repo_slug)


def acquire_lock(repo_slug: str, home: Path | None = None, config: LoopConfig = DEFAULTS):
    """Take the single-supervisor lock, or return ``None`` if another holds it."""
    path = ledger_path(repo_slug, home, config).with_suffix(".lock")
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
    if record.get("orphaned"):
        # decide() escalates an orphaned run before anything else, so it gets its
        # own identity and is not held back under the pause it was sent for.
        return f"{run}:orphaned"
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


# ------------------------------------------------------------------ command line


def _read_json(source: str | None) -> tuple[bool, object]:
    """``(ok, value)``: no file, an empty file or ``null`` is ``(True, None)``."""
    if source is None:
        return (True, None)
    try:
        raw = Path(source).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return (False, None)
    if not raw or raw == "null":
        return (True, None)
    try:
        return (True, json.loads(raw))
    except ValueError:
        return (False, None)


def _hold(handle: int) -> int:
    """Keep the lock until a signal ends this process; the kernel then frees it."""

    def _stop(_signum: int, _frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    print(f"LOCKED: pid {os.getpid()}", flush=True)
    try:
        while True:
            signal.pause()
    finally:
        os.close(handle)


def _unlock(repo_slug: str, config: LoopConfig) -> int:
    """End the process that holds the lock, by the pid the lock file records."""
    lock = ledger_path(repo_slug, config=config).with_suffix(".lock")
    try:
        pid = int(lock.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pid = None
    handle = acquire_lock(repo_slug, config=config)
    if handle is not None:
        os.close(handle)
        print("NOT_LOCKED: no supervisor holds the lock")
        return 0
    if pid is None:
        print(f"LOCK_UNREADABLE: {lock}", file=sys.stderr)
        return 2
    os.kill(pid, signal.SIGTERM)
    print(f"UNLOCKED: sent the end signal to pid {pid}")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loop-ledger", description="The supervisor's lock and notification ledger."
    )
    parser.add_argument("--repo", required=True, help="the owner/name the supervisor watches")
    parser.add_argument("--config", help="a loop.toml, or its directory, that moves the ledger")
    sub = parser.add_subparsers(dest="verb", required=True)
    sub.add_parser("lock", help="take the lock and hold it until this process is ended")
    sub.add_parser("unlock", help="end the process that holds the lock")
    sub.add_parser("path", help="print the ledger file's path")
    for verb, text in (
        ("check", "exit 0 when this pause should be notified on this channel, 1 when not"),
        ("record", "record one delivered notification"),
    ):
        one = sub.add_parser(verb, help=text)
        one.add_argument("--record-file", required=True, help="one loop-scan record")
        one.add_argument("--question-file", help="a loop-transcript result")
        one.add_argument("--channel", required=True, help="where it goes, such as desktop")
        one.add_argument("--now", help="ISO-8601 override for tests")
        if verb == "check":
            one.add_argument("--reescalate-after", type=int, default=1800, help="seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    config = load_cli_config(args.config)
    if config is None:
        return 2
    try:
        slug = ps.repo_slug(args.repo)
        path = ledger_path(slug, config=config)
    except ValueError as exc:
        print(f"MALFORMED_INPUT: {exc}", file=sys.stderr)
        return 2
    if args.verb == "path":
        print(path)
        return 0
    if args.verb == "lock":
        return _lock(slug, config, path)
    if args.verb == "unlock":
        return _unlock(slug, config)
    return _check_or_record(args, path)


def _lock(repo_slug: str, config: LoopConfig, path: Path) -> int:
    handle = acquire_lock(repo_slug, config=config)
    if handle is None:
        print(f"LOCK_HELD: another supervisor holds {path.with_suffix('.lock')}")
        return 1
    return _hold(handle)


def _check_or_record(args: argparse.Namespace, path: Path) -> int:
    ok_record, record = _read_json(args.record_file)
    ok_question, question = _read_json(args.question_file)
    if not ok_record or not isinstance(record, dict) or not ok_question:
        print("MALFORMED_INPUT: --record-file or --question-file did not parse", file=sys.stderr)
        return 2
    key = pause_key(record, question if isinstance(question, dict) else None)
    now = _parse(args.now) or datetime.now().astimezone()
    if args.verb == "record":
        append_notification(path, key, args.channel, now)
        print(json.dumps({"key": key, "channel": args.channel, "recorded": True}))
        return 0
    notify = should_notify(read_ledger(path), key, args.channel, now, args.reescalate_after)
    print(json.dumps({"key": key, "channel": args.channel, "notify": notify}))
    return 0 if notify else 1


if __name__ == "__main__":
    raise SystemExit(main())
