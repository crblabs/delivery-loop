#!/usr/bin/env python3
"""Discover every delivery-loop run and report its state, read-only.

The supervisor watches every run at once. Every run has a directory under the
state root, ``~/.delivery-loop`` unless ``$DELIVERY_LOOP_HOME`` or the config
moves it: ``runs/<owner>-<repo>/<worktree>-<hash>/``. A directory that holds a
state file is a run, so discovery needs no glob and no repository path. Git is
used only to find each run's config and the operator's user file.

Each run directory records the worktree it belongs to. A run whose record is
missing reads as ``unreadable``. A run whose worktree no longer exists is marked
``orphaned`` and needs attention until ``loop-prune`` removes it. A run the
plugin drives (``driver: plugin``) that is still active but has no run index
entry is marked ``index_missing``: its hooks can no longer find it, so nothing
enforces its guard.

Without ``--config``, the command reads its own config from the repository it
runs in, over the operator's user file, and each run is then read under the
config of the worktree it records: that worktree's loop.toml over the user file.
A run is then checked against the stage list its own branch declares. A run
whose own config is not valid is reported with the condition ``config_invalid``
and an ``error``, and the scan goes on to the next run. With ``--config``, the
named file applies to every run.

Discovery binds to one repository: a run whose ``repo`` differs from ``--repo``
is excluded, so an allowlist or ledger for one repository never authorizes
another.

Output is a JSON array on stdout, one object per run. Exit codes follow the
monitor convention: ``0`` observed and clear, ``1`` observed and at least one run
needs attention, ``2`` cannot observe.

The tool never writes and never touches the network.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from core import pipeline_state as ps
from core import run_index as ri
from core.config import (
    DEFAULTS,
    ConfigError,
    LoopConfig,
    config_for_run,
    from_dict,
    load_cli_config,
)

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
    "driver",
    "waiting_on",
    "waiting_since",
    "waiting_shell_only",
)


def discover(config: LoopConfig = DEFAULTS) -> list[tuple[Path, Path | None, Path]]:
    """Every run under the state root, as (run directory, worktree, state file).

    A directory counts as a run when its state file exists. The worktree is
    ``None`` when the directory does not record one.
    """
    runs = ps.state_home(config) / ps.RUNS_DIR
    found = []
    for directory in sorted(runs.glob("*/*")):
        if directory.is_symlink() or not directory.is_dir():
            continue
        state_file = directory / config.state_file
        if state_file.exists():
            found.append((directory, ps.read_worktree(directory), state_file))
    return found


def _age_seconds(updated_at: object, now: datetime) -> float | None:
    dt = ps.parse_iso(updated_at)
    return None if dt is None else (now - dt).total_seconds()


def _activity_age(path: object, now: datetime) -> float | None:
    """Seconds since the session last wrote its activity file, or None when unknown."""
    if not isinstance(path, str) or not path:
        return None
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    return now.timestamp() - mtime


def _wait_stale(
    state: dict,
    now: datetime,
    config: LoopConfig,
    wait_stale_after: int | None = None,
    shell_wait_stale_after: int | None = None,
) -> bool:
    """Whether a running run's wait has lasted past its limit with the session quiet.

    The limit is the run's own config (``[supervisor]``) unless the command line
    sets one; a wait on shell commands alone gets the shorter one. Quiet means no
    turn end and, when the harness names one, no write to its activity file: a
    turn a notification woke writes the transcript while it works.
    """
    if state.get("status") != "running" or not state.get("waiting_on"):
        return False
    if state.get("waiting_shell_only"):
        limit = shell_wait_stale_after or config.shell_wait_stale_after_s
    else:
        limit = wait_stale_after or config.wait_stale_after_s
    wait_age = _age_seconds(state.get("waiting_since"), now)
    age = _age_seconds(state.get("updated_at"), now)
    activity = _activity_age(state.get("activity_path"), now)
    return (
        wait_age is not None
        and wait_age >= limit
        and age is not None
        and age >= limit
        and (activity is None or activity >= limit)
    )


def build_record(
    directory: Path,
    worktree: Path | None,
    state_file: Path,
    now: datetime,
    stale_after: int,
    config: LoopConfig = DEFAULTS,
    per_worktree: bool = False,
    wait_stale_after: int | None = None,
    shell_wait_stale_after: int | None = None,
) -> dict:
    """One report object for one run, whatever the file's condition.

    A run that does not record its worktree reads as ``unreadable``, because
    nothing ties it to a place a person can act on. With ``per_worktree``, a run
    whose worktree still exists is read under that worktree's own config.
    """
    base = {
        "run_dir": str(directory),
        "worktree": None if worktree is None else str(worktree),
        "orphaned": worktree is not None and not worktree.exists(),
    }
    if worktree is None:
        return {**base, "condition": "unreadable"} | _raw_repo(state_file)
    if per_worktree and worktree.exists():
        try:
            # A run the plugin started records the config it runs under; judge it
            # by that, as its hooks do, not by whatever loop.toml says now.
            config = _snapshot(directory) or config_for_run(str(worktree), config, explicit=False)
        except ConfigError as exc:
            # The repository is kept when the state file names one, so --repo can
            # still drop a broken run that belongs to another repository.
            return {**base, "condition": "config_invalid", "error": str(exc)} | _raw_repo(
                state_file
            )
    condition, state = ps.read_state(state_file, config)
    record = {**base, "condition": condition}
    if state is None:
        # The repository is kept when the state file names one, so --repo can
        # drop an unreadable run that belongs to another repository.
        return record | _raw_repo(state_file)
    for key in _RECORD_KEYS:
        record[key] = state.get(key)
    record["stage_num"] = state["current"] + 1
    # How a run reads as "stage 2 of N", with N read off the configured list.
    record["stage_count"] = len(config.stages)
    record["history_len"] = len(state.get("history", []))
    age = _age_seconds(state.get("updated_at"), now)
    record["age_seconds"] = age
    record["is_stale"] = age is not None and age >= stale_after
    # A wait is judged by when it began, not by updated_at: every turn end
    # writes the state, so a long wait can look fresh. It is stale only when the
    # session is quiet too: waiting_on changes at a turn end, so a turn a
    # notification woke still lists the task while it works. A paused run keeps
    # its own pause card.
    record["wait_age_s"] = (
        _age_seconds(state.get("waiting_since"), now) if state.get("waiting_on") else None
    )
    record["is_wait_stale"] = _wait_stale(
        state, now, config, wait_stale_after, shell_wait_stale_after
    )
    record["index_missing"] = _index_missing(directory, worktree, state)
    return record


def _index_missing(directory: Path, worktree: Path, state: dict) -> bool:
    """Whether an active plugin run has lost the index entry its hooks find it by."""
    if state.get("driver") != "plugin" or state.get("status") not in ri.ACTIVE:
        return False
    entry = ri.read_entry(ri.index_path(ri.worktree_key(str(worktree))))
    return entry is None or Path(entry["run_dir"]) != directory


def _snapshot(directory: Path) -> LoopConfig | None:
    """The config a plugin run started under, or ``None`` for a run with none."""
    path = directory / ri.SNAPSHOT_FILE
    if not path.exists():
        return None
    data = ri.read_json_file(str(path), ri.SNAPSHOT_MAX_BYTES)
    if data is None:
        raise ConfigError(f"{path} cannot be read")
    return from_dict(data)


def _needs_attention(record: dict) -> bool:
    return (
        record.get("condition") != "ok"
        or record.get("status") != "running"
        or bool(record.get("is_stale"))
        or bool(record.get("is_wait_stale"))
        or bool(record.get("orphaned"))
        or bool(record.get("index_missing"))
    )


def scan(
    repo: str | None,
    now: datetime,
    stale_after: int,
    config: LoopConfig = DEFAULTS,
    per_worktree: bool = False,
    wait_stale_after: int | None = None,
    shell_wait_stale_after: int | None = None,
) -> list[dict]:
    """Discover, repo-filter, and report every run."""
    records = [
        build_record(
            directory,
            worktree,
            state_file,
            now,
            stale_after,
            config,
            per_worktree,
            wait_stale_after,
            shell_wait_stale_after,
        )
        for directory, worktree, state_file in discover(config)
    ]
    if repo is not None:
        records = [r for r in records if _repo_ok(r, repo)]
    return records


def _repo_ok(record: dict, repo: str) -> bool:
    # A readable run must match the repo; an unreadable one is kept so a broken
    # run is still surfaced, never silently dropped, unless it names another
    # repository.
    if record.get("condition") == "ok":
        return record.get("repo") == repo
    return record.get("repo") in (None, repo)


def _raw_repo(state_file: Path) -> dict:
    """The repository a state file names, read without validating the rest."""
    try:
        repo = json.loads(state_file.read_text(encoding="utf-8")).get("repo")
    except (OSError, ValueError, AttributeError, RecursionError):
        return {}
    return {"repo": repo} if isinstance(repo, str) else {}


def _now_from(arg: str | None) -> datetime:
    if arg is None:
        return datetime.now(UTC)
    parsed = ps.parse_iso(arg)
    if parsed is None:
        raise SystemExit("--now must be an ISO-8601 timestamp with an offset")
    return parsed


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Report every delivery-loop run.")
    parser.add_argument("--repo", default=None, help="owner/name to bind discovery to")
    parser.add_argument("--now", default=None, help="ISO-8601 override for tests")
    parser.add_argument("--stale-after-seconds", type=int, default=DEFAULT_STALE_S)
    parser.add_argument(
        "--wait-stale-after-seconds",
        type=int,
        default=None,
        help="how long a wait on background tasks, and the quiet since the last turn "
        "end, may last before it is flagged (default: the run's [supervisor] "
        "wait_stale_after_seconds, 3600)",
    )
    parser.add_argument(
        "--shell-wait-stale-after-seconds",
        type=int,
        default=None,
        help="the same limit for a wait on background shell commands alone (default: the "
        "run's [supervisor] shell_wait_stale_after_seconds, 900)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help=(
            "a loop.toml, or its directory, applied to every run (default: each run's own"
            " worktree config)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    # The config is loaded once here and passed down. It gives the state root.
    # With no --config, each run is then read under the config of its worktree.
    config = load_cli_config(args.config, "SUPERVISOR_CONFIG_INVALID")
    if config is None:
        return 2
    records = scan(
        args.repo,
        _now_from(args.now),
        args.stale_after_seconds,
        config,
        per_worktree=args.config is None,
        wait_stale_after=args.wait_stale_after_seconds,
        shell_wait_stale_after=args.shell_wait_stale_after_seconds,
    )
    print(json.dumps(records, indent=2, sort_keys=True))
    if not records:
        print(
            f"SUPERVISOR_DISCOVERY_EMPTY: no run under {ps.state_home(config) / ps.RUNS_DIR}",
            file=sys.stderr,
        )
        return 2
    return 1 if any(_needs_attention(r) for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
