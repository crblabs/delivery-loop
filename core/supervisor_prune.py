#!/usr/bin/env python3
"""List, and on request delete, the runs whose worktree no longer exists.

A run's state lives under the state root, outside the worktree, so removing a
worktree leaves its run behind. ``loop-scan`` marks such a run ``orphaned``.
This tool lists every orphaned run and, with ``--yes``, deletes its directory.

Only a run that records its worktree, and whose worktree is gone, is ever
deleted. A run that records no worktree is left for a person to inspect.

Output is a JSON array on stdout, one object per orphaned run. Exit code is
``0`` when the listing (and any deletion) succeeded, ``1`` when a deletion
failed, and ``2`` when the config cannot be read.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from core import pipeline_state as ps
from core import supervisor_scan as ss
from core.config import DEFAULTS, ConfigError, LoopConfig, load_config


def orphaned(config: LoopConfig = DEFAULTS) -> list[tuple[Path, Path]]:
    """Every (run directory, recorded worktree) whose worktree no longer exists."""
    return [
        (directory, worktree)
        for directory, worktree, _ in ss.discover(config)
        if worktree is not None and not worktree.exists()
    ]


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List the delivery-loop runs whose worktree is gone, and delete them."
    )
    parser.add_argument("--yes", action="store_true", help="delete the listed run directories")
    parser.add_argument("--config", default=None, help="a loop.toml, or the directory holding one")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, config: LoopConfig = DEFAULTS) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = load_config(args.config) if args.config else config
    except ConfigError as exc:
        print(f"PRUNE_CONFIG_INVALID: {exc}", file=sys.stderr)
        return 2
    runs_root = (ps.state_home(config) / ps.RUNS_DIR).resolve()
    report = []
    failed = False
    for directory, worktree in orphaned(config):
        entry = {"run_dir": str(directory), "worktree": str(worktree), "deleted": False}
        # Never delete outside the runs root, whatever a directory name resolves to.
        if args.yes and directory.resolve().parent.parent == runs_root:
            try:
                shutil.rmtree(directory)
                entry["deleted"] = True
            except OSError as exc:
                print(f"PRUNE_FAILED: {directory}: {exc}", file=sys.stderr)
                failed = True
        report.append(entry)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
