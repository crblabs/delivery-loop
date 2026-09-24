#!/usr/bin/env python3
"""Discover every delivery-loop run and report its state, read-only.

The supervisor watches every run at once. Runs are found by a glob over worktree
state files. ``DEFAULT_GLOB`` is only a default: it matches the common layout
where worktrees are nested one level below a per-repository container. Any other
layout is passed with ``--worktrees-glob``.

Discovery binds to one repository: a worktree whose ``repo`` differs from
``--repo`` is excluded, so an allowlist or ledger for one repository never
authorizes another.

Output is a JSON array on stdout, one object per run. Exit codes follow the
monitor convention: ``0`` observed and clear, ``1`` observed and at least one run
needs attention, ``2`` cannot observe.

The tool never writes and never touches the network.
"""

from __future__ import annotations

import argparse
import glob as globlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from core import pipeline_state as ps

# A default for one common worktree layout, not a fact about any one
# machine: --worktrees-glob replaces it wholesale.
DEFAULT_GLOB = "~/emdash/worktrees/*/*/.claude/pipeline.local.json"
DEFAULT_STALE_S = 600
_RECORD_KEYS = (
    "run_id",
    "branch",
    "session_id",
    "repo",
    "task",
    "task_title",
    "current_stage",
    "status",
    "attempts",
    "total_attempts",
    "caps",
    "paused_reason",
    "paused_prompt_id",
    "pending_question",
    "pending_promotion",
    "promotion_asked",
    "guard_pending",
    "guard_files_seen",
    "guard_baseline",
    "revision",
    "started_at",
    "updated_at",
    "hook_seen",
    "plan_path",
    "pr_url",
)


def discover(pattern: str) -> list[Path]:
    """Every state-file path the glob matches that is a regular file."""
    expanded = str(Path(pattern).expanduser())
    hits = sorted({Path(p) for p in globlib.glob(expanded) if Path(p).is_file()})
    return hits


def _age_seconds(updated_at: object, now: datetime) -> float | None:
    dt = ps.parse_iso(updated_at)
    return None if dt is None else (now - dt).total_seconds()


def build_record(state_file: Path, now: datetime, stale_after: int) -> dict:
    """One report object for one worktree, whatever the file's condition."""
    worktree = str(state_file.parent.parent)
    condition, state = ps.read_state(state_file)
    if state is None:
        return {"worktree": worktree, "condition": condition}
    record = {"worktree": worktree, "condition": condition}
    for key in _RECORD_KEYS:
        record[key] = state.get(key)
    record["stage_num"] = state["current"] + 1
    record["history_len"] = len(state.get("history", []))
    age = _age_seconds(state.get("updated_at"), now)
    record["age_seconds"] = age
    record["is_stale"] = age is not None and age >= stale_after
    return record


def _needs_attention(record: dict) -> bool:
    return (
        record.get("condition") != "ok"
        or record.get("status") != "running"
        or bool(record.get("is_stale"))
    )


def scan(pattern: str, repo: str | None, now: datetime, stale_after: int) -> list[dict]:
    """Discover, repo-filter, and report every run."""
    records = [build_record(p, now, stale_after) for p in discover(pattern)]
    if repo is not None:
        records = [r for r in records if _repo_ok(r, repo)]
    return records


def _repo_ok(record: dict, repo: str) -> bool:
    # A readable run must match the repo; an unreadable one is kept so a broken
    # worktree in this tree is still surfaced, never silently dropped.
    return record.get("condition") != "ok" or record.get("repo") == repo


def _now_from(arg: str | None) -> datetime:
    if arg is None:
        return datetime.now(UTC)
    parsed = ps.parse_iso(arg)
    if parsed is None:
        raise SystemExit("--now must be an ISO-8601 timestamp with an offset")
    return parsed


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report every delivery-loop run.")
    parser.add_argument(
        "--worktrees-glob",
        default=DEFAULT_GLOB,
        help="state-file glob; the default matches one common worktree layout",
    )
    parser.add_argument("--repo", default=None, help="owner/name to bind discovery to")
    parser.add_argument("--now", default=None, help="ISO-8601 override for tests")
    parser.add_argument("--stale-after-seconds", type=int, default=DEFAULT_STALE_S)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    records = scan(args.worktrees_glob, args.repo, _now_from(args.now), args.stale_after_seconds)
    print(json.dumps(records, indent=2, sort_keys=True))
    if not records:
        print(
            f"SUPERVISOR_DISCOVERY_EMPTY: no worktree held a state file for {args.worktrees_glob}",
            file=sys.stderr,
        )
        return 2
    return 1 if any(_needs_attention(r) for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
