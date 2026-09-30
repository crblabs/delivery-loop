#!/usr/bin/env python3
"""Discover every delivery-loop run and report its state, read-only.

The supervisor watches every run at once. Runs are found through
``git worktree list``: every worktree of a repository whose git directory holds a
state file is a run. No layout is assumed, so worktrees may sit anywhere on disk.
``--repo-dir`` names a directory inside the repository, and may be given once
per repository. It defaults to the current directory.

Listing worktrees with ``-z`` needs git 2.36 or later. An older git is reported as
cannot observe, with the version it needs.

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
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from core import pipeline_state as ps
from core.config import DEFAULTS, LoopConfig, load_config

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


class ScanError(RuntimeError):
    """A repository the scanner cannot list the worktrees of."""


# git exits 129 on an option it does not know. `worktree list -z` needs 2.36.
_GIT_USAGE_EXIT = 129
MIN_GIT = "2.36"


def _git(repo_dir: Path, *args: str) -> str:
    try:
        result = ps.run_git(repo_dir, *args)
    except OSError as exc:
        raise ScanError(f"cannot run git for {repo_dir}: {exc}") from exc
    if result.returncode == _GIT_USAGE_EXIT:
        raise ScanError(f"loop-scan needs git {MIN_GIT} or later: {result.stderr.strip()}")
    if result.returncode != 0:
        raise ScanError(f"cannot read the repository at {repo_dir}: {result.stderr.strip()}")
    return result.stdout


def worktrees(repo_dir: Path) -> list[Path]:
    """Every checked-out worktree of the repository that holds ``repo_dir``.

    A bare entry has no working tree and a prunable one no longer exists on
    disk, so neither can hold a run.
    """
    output = _git(repo_dir, "worktree", "list", "--porcelain", "-z")
    found: list[Path] = []
    # One NUL ends each line and an empty line ends each worktree entry.
    for entry in output.split("\0\0"):
        lines = [line for line in entry.split("\0") if line]
        if not lines or not lines[0].startswith("worktree "):
            continue
        if any(line == "bare" or line.startswith("prunable") for line in lines[1:]):
            continue
        found.append(Path(lines[0].removeprefix("worktree ")))
    return found


def common_dir(repo_dir: Path) -> Path:
    """The git directory every worktree of the repository shares."""
    output = _git(repo_dir, "rev-parse", "--path-format=absolute", "--git-common-dir")
    return Path(output.strip()).resolve()


def _belongs(git_directory: Path, common: Path) -> bool:
    # The main worktree's git directory is the common one; a linked worktree's
    # sits one level below its `worktrees` directory.
    return git_directory == common or git_directory.parent == common / "worktrees"


def discover(
    repo_dirs: list[Path], config: LoopConfig = DEFAULTS
) -> list[tuple[Path, Path | None]]:
    """Every (worktree, state file) pair whose state file is a regular file.

    A listed worktree whose git directory cannot be resolved, or resolves
    outside the scanned repository, is returned with ``None`` for its state
    file, so the report surfaces it rather than dropping it.
    """
    hits: dict[Path, Path | None] = {}
    for repo_dir in repo_dirs:
        common = common_dir(repo_dir)
        for worktree in worktrees(repo_dir):
            try:
                git_directory = ps.git_dir(worktree)
            except ps.NotAWorktree:
                hits[worktree] = None
                continue
            if not _belongs(git_directory, common):
                hits[worktree] = None
                continue
            path = ps.state_path_in(git_directory, config)
            if path.is_file():
                hits[worktree] = path
    return sorted(hits.items())


def _age_seconds(updated_at: object, now: datetime) -> float | None:
    dt = ps.parse_iso(updated_at)
    return None if dt is None else (now - dt).total_seconds()


def build_record(
    worktree_path: Path,
    state_file: Path | None,
    now: datetime,
    stale_after: int,
    config: LoopConfig = DEFAULTS,
) -> dict:
    """One report object for one worktree, whatever the file's condition.

    ``state_file`` is ``None`` when the worktree's git directory could not be
    trusted, and the run reads as ``unreadable``.
    """
    worktree = str(worktree_path)
    if state_file is None:
        return {"worktree": worktree, "condition": "unreadable"}
    condition, state = ps.read_state(state_file, config)
    if state is None:
        return {"worktree": worktree, "condition": condition}
    record = {"worktree": worktree, "condition": condition}
    for key in _RECORD_KEYS:
        record[key] = state.get(key)
    record["stage_num"] = state["current"] + 1
    # How a run reads as "stage 2 of N", with N read off the configured list.
    record["stage_count"] = len(config.stages)
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


def scan(
    repo_dirs: list[Path],
    repo: str | None,
    now: datetime,
    stale_after: int,
    config: LoopConfig = DEFAULTS,
) -> list[dict]:
    """Discover, repo-filter, and report every run."""
    records = [
        build_record(wt, path, now, stale_after, config) for wt, path in discover(repo_dirs, config)
    ]
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
        "--repo-dir",
        action="append",
        type=Path,
        default=None,
        help="a directory inside a repository to scan; repeat it for more; default: .",
    )
    parser.add_argument("--repo", default=None, help="owner/name to bind discovery to")
    parser.add_argument("--now", default=None, help="ISO-8601 override for tests")
    parser.add_argument("--stale-after-seconds", type=int, default=DEFAULT_STALE_S)
    parser.add_argument("--config", default=None, help="a loop.toml, or the directory holding one")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, config: LoopConfig = DEFAULTS) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    # The config is loaded once here and passed down; nothing below reads a file.
    config = load_config(args.config) if args.config else config
    repo_dirs = args.repo_dir or [Path.cwd()]
    try:
        records = scan(repo_dirs, args.repo, _now_from(args.now), args.stale_after_seconds, config)
    except ScanError as exc:
        print(f"SUPERVISOR_DISCOVERY_FAILED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(records, indent=2, sort_keys=True))
    if not records:
        names = ", ".join(str(d) for d in repo_dirs)
        print(
            f"SUPERVISOR_DISCOVERY_EMPTY: no worktree held a state file for {names}",
            file=sys.stderr,
        )
        return 2
    return 1 if any(_needs_attention(r) for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
