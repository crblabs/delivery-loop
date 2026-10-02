"""loop-issue: lint an issue draft, then write it to the tracker.

The one write path to the tracker. The guard denies a tracker's own write
tools during a run, and this command refuses ``--apply`` and ``--update``
during one too: a run writes only to its worktree and its pull request, so an
issue it finds goes in the pull request body as a draft a person files later.

  loop-issue DRAFT.md                      # lint only (the default)
  loop-issue DRAFT.md --apply              # create the issue
  loop-issue DRAFT.md --update             # update the issue named by `issue:`
  loop-issue --audit                       # portfolio audit against the baseline
  loop-issue --audit --update-baseline     # seed or lower the baseline

``--tracker`` picks the adapter; ``linear`` is the default and the only one
today. The lint without a key checks the shape and that each name is present;
with the tracker's key in the environment it also resolves the names. A write
and the audit need the key. The draft format and the rules are in
``core.issue_draft``. The baseline is ``.issue-audit-baseline.json`` at the
root of the repository the command runs in, unless ``--baseline`` names one.

Exit codes follow the shared tools contract: 0 the draft is clean, the issue was
written, or the audit holds; 1 the draft is refused or a count rose; 2 a usage
or environment fault, such as a missing key or a failed read.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path

from core import issue_draft as idr
from core import run_index as ri
from core.config import LoopConfig, load_cli_config, repo_root

# Each tracker is a package under adapters/ with a `connect(environ, required)`.
TRACKERS = {"linear": "adapters.linear"}
DEFAULT_TRACKER = "linear"
BASELINE_FILE = ".issue-audit-baseline.json"


def connect(name: str, required: bool) -> idr.Tracker | None:
    """The tracker client, or ``None`` when its key is absent and not required."""
    return import_module(TRACKERS[name]).connect(os.environ, required)


def run_active(cwd: str) -> bool:
    """Whether a run is active in the worktree ``cwd`` is in. A state that
    cannot be read counts as active, so a broken run never opens the gate."""
    found = ri.find_entry(cwd)
    if found is None:
        return False
    state = ri.read_json_file(ri.state_file(found[1]["run_dir"]), ri.STATE_MAX_BYTES)
    return not isinstance(state, dict) or state.get("status") in ri.ACTIVE


def _print_lines(lines: list[str]) -> int:
    for line in lines:
        print(line)
    return 1


def cmd_check(path: Path, config: LoopConfig, tracker_name: str) -> int:
    draft = idr.read_draft(path, config)
    tracker = connect(tracker_name, required=False)
    problems = idr.lint_offline(draft, config)
    if tracker is not None:
        problems += idr.resolve(tracker, draft)[1]
    if problems:
        return _print_lines(problems)
    print("ok" if tracker is not None else "ok (shape clean; names not resolved, no key set)")
    return 0


def cmd_write(path: Path, config: LoopConfig, tracker_name: str, update: bool) -> int:
    if run_active(os.getcwd()):
        print(
            "refused: a delivery-loop run is active here, and a run writes only to its "
            "worktree and its pull request. Put the draft in the pull request body; a "
            "person files it after the run."
        )
        return 1
    draft = idr.read_draft(path, config)
    reference = draft.get("issue")
    if update and not reference:
        raise idr.DraftError("--update needs an `issue:` line naming the issue to update.")
    tracker = connect(tracker_name, required=True)
    ids, unresolved = idr.resolve(tracker, draft)
    problems = idr.lint_offline(draft, config) + unresolved
    if problems:
        return _print_lines(problems)
    issue = idr.issue_input(draft, ids)
    written = tracker.update_issue(reference, issue) if update else tracker.create_issue(issue)
    print(f"wrote {written.identifier} {written.url}")
    return 0


def _audit_verdict(risen: list[str], baseline: dict[str, int] | None) -> int:
    if baseline is None:
        print("no baseline yet; run --audit --update-baseline once to seed it.")
        return 0
    if risen:
        print(f"audit failed: {', '.join(risen)} rose above baseline.")
        return 1
    print("audit green.")
    return 0


def cmd_audit(tracker_name: str, baseline_path: Path, update_baseline: bool) -> int:
    tracker = connect(tracker_name, required=True)
    baseline = idr.load_baseline(baseline_path, tracker_name)
    counts = idr.run_audit(tracker)
    for key in idr.RULE_KEYS:
        print(f"{key}={counts[key]} (baseline {'n/a' if baseline is None else baseline[key]})")
    risen = idr.ratchet(counts, baseline)
    if not update_baseline:
        return _audit_verdict(risen, baseline)
    if risen:
        print(f"refused: {', '.join(risen)} rose above baseline; not seeding.")
        return 1
    today = datetime.now(UTC).date().isoformat()
    idr.write_baseline(baseline_path, tracker_name, counts, today)
    print(f"baseline updated: {baseline_path}")
    return 0


def _baseline_path(args: argparse.Namespace) -> Path:
    if args.baseline:
        return Path(args.baseline)
    return repo_root(Path.cwd()) / BASELINE_FILE


def _dispatch_audit(args: argparse.Namespace) -> int:
    if args.draft or args.apply or args.update:
        raise idr.DraftError("--audit takes no draft and no --apply or --update.")
    return cmd_audit(args.tracker, _baseline_path(args), args.update_baseline)


def dispatch(args: argparse.Namespace, config: LoopConfig) -> int:
    if args.audit:
        return _dispatch_audit(args)
    if args.update_baseline:
        raise idr.DraftError("--update-baseline goes with --audit.")
    if not args.draft:
        raise idr.DraftError("give a draft file, or --audit.")
    if args.apply and args.update:
        raise idr.DraftError("choose one of --apply or --update, not both.")
    path = Path(args.draft)
    if args.apply or args.update:
        return cmd_write(path, config, args.tracker, update=args.update)
    return cmd_check(path, config, args.tracker)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loop-issue", description="Lint an issue draft, write it, or audit the tracker."
    )
    parser.add_argument("draft", nargs="?", help="the draft Markdown file")
    parser.add_argument("--apply", action="store_true", help="create the issue")
    parser.add_argument("--update", action="store_true", help="update the issue `issue:` names")
    parser.add_argument("--audit", action="store_true", help="run the portfolio audit")
    parser.add_argument("--update-baseline", action="store_true", help="seed or lower it")
    parser.add_argument(
        "--tracker", choices=sorted(TRACKERS), default=DEFAULT_TRACKER, help="the tracker"
    )
    parser.add_argument("--baseline", help=f"the baseline file (default {BASELINE_FILE})")
    parser.add_argument("--config", help="a loop.toml, or a directory that holds one")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    config = load_cli_config(args.config)
    if config is None:
        return 2
    try:
        return dispatch(args, config)
    except idr.DraftError as exc:
        print(f"usage: {exc}", file=sys.stderr)
        return 2
    except idr.TrackerError as exc:
        print(f"environment: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
