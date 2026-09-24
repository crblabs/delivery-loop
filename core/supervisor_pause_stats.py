#!/usr/bin/env python3
"""Classify delivery-loop pauses by type, read-only.

The supervisor auto-answer targets exactly one pause: a ``guard_changed`` pause
whose changed loop files are all on the operator launch allowlist and none is an
enforcement carve-out. This reporter counts pauses by type so the operator can
see how large that removable share really is before any push is built. A guard
pause fires only on an undeclared loop edit (the hook auto-continues a declared
one), so this measurement, not the raw pause count, is the return on the change.

Input is the JSON array ``supervisor_scan`` prints, on stdin or ``--scan-file``.
The tool never writes and never touches the network. Exit code is ``0`` on a
readable input and ``2`` when the input cannot be parsed.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from core import supervisor_decide as sd

CATEGORIES = (
    "removable_auto_accept",
    "undeclared_off_allowlist",
    "carveout",
    "guard_irregular",
    "guard_untrusted",
    "pause_promotion",
    "pause_needs_human",
    "pause_branch_mismatch",
    "pause_no_message",
    "pause_other",
    "unobservable",
    "not_paused",
)


def classify_pause(record: object, allowlist: list[str]) -> str:
    """One category for one scan record. See ``CATEGORIES`` for the vocabulary."""
    if not isinstance(record, dict):
        return "unobservable"
    if record.get("condition") != "ok":
        return "unobservable"
    if record.get("status") != "awaiting_human":
        return "not_paused"
    # The promotion floor is checked before status in decide(), so a pending
    # promotion is never removable however the guard paths look.
    if record.get("pending_promotion"):
        return "pause_promotion"
    reason = record.get("paused_reason")
    if reason != "guard_changed":
        known = {"needs_human", "branch_mismatch", "no_message"}
        return f"pause_{reason}" if reason in known else "pause_other"
    return _classify_guard(record, allowlist)


def _classify_guard(record: dict, allowlist: list[str]) -> str:
    paths = sd.changed_guard_paths(record)
    if paths is None:
        return "guard_untrusted"
    if sd._has_unsafe_value(record, paths):
        return "guard_irregular"
    if any(sd._is_carveout(p) for p in paths):
        return "carveout"
    if sd._all_authorized(paths, allowlist):
        return "removable_auto_accept"
    return "undeclared_off_allowlist"


def summarize(records: list, allowlist: list[str]) -> dict[str, int]:
    """Counts by category over every record, every category key present."""
    counts = Counter(classify_pause(r, allowlist) for r in records)
    return {category: counts.get(category, 0) for category in CATEGORIES}


def _load(scan_file: str | None) -> object:
    try:
        if scan_file is None:
            return json.loads(sys.stdin.read() or "null")
        with open(scan_file, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Count delivery-loop pauses by type.")
    parser.add_argument(
        "--scan-file", default=None, help="supervisor_scan.py output; stdin if unset"
    )
    parser.add_argument("--allow-path", action="append", default=[])
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    records = _load(args.scan_file)
    if not isinstance(records, list):
        print("MALFORMED_INPUT: expected a JSON array of scan records", file=sys.stderr)
        return 2
    summary = summarize(records, args.allow_path)
    print(json.dumps({"total": len(records), "by_category": summary}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
