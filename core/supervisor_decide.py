#!/usr/bin/env python3
"""The v1 supervisor decision policy, a pure function.

``decide(record, allowlist, question, notify_only)`` maps one scan record, the
launch allowlist, and any pending transcript question to a ``Decision``. It is
pure and takes no ledger state: whether a pause was already notified, and
re-escalation timing, are scheduling, not policy, and live in the routine's
ledger layer. ``decide()`` classifies one run's current state and nothing else.

The floor, enforced first and never crossed: no data promotion, no failed-run
resume, no plan approval, no answer that widens a task. When the floor blocks an
action the decision is always ``escalate``.

A ``permission_prompt`` question (a run parked on a harness permission prompt)
escalates whatever the state file status is, ``done`` included. It is checked
after the floors, so a floor keeps its own reason, and before the status
branches, so no status can hide it.

Auto-answer authorization rests only on the ``--allow-path`` allowlist and a
structured, non-empty diff of the pending guard map against the last accepted
map. Plan prose is never an input. On today's hook the qa-continue route has no
machine-readable discriminator, so it escalates; and ``--notify-only`` collapses
every auto-answer to ``escalate``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path, PurePosixPath

# The carve-out list has one home; import it rather than copy it, so the
# supervisor and the guard cannot disagree on what is an enforcement file.
from core.pipeline_loop_paths import is_carveout as _is_carveout

ACTIONS = ("auto_accept", "auto_continue", "escalate", "noop")


def _unsafe(path: object) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/"):
        return True
    return any(ord(c) < 0x20 for c in path)


def normalize(path: str) -> str | None:
    """One repo-relative form, or ``None`` for an unsafe path.

    The check is lexical: it rejects absolute, ``..``, and control-character
    paths, but it does not resolve a symlink target against the worktree
    boundary, because ``decide()`` is pure and holds no worktree root. In v1 the
    auto-accept path is gated off (the routine always passes ``--notify-only``),
    so a symlinked guard path never reaches an auto-answer; the resolved-target
    check lands together with the auto-answer path.
    """
    if _unsafe(path):
        return None
    parts = [p for p in PurePosixPath(path).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        return None
    return str(PurePosixPath(*parts)) if parts else None


def changed_guard_paths(record: dict) -> list[str] | None:
    """Paths whose hash differs from the last accepted map.

    Returns ``None`` when the evidence cannot be trusted (no pending map, or a
    missing or malformed accepted baseline). A caller must escalate on ``None``,
    never diff against an empty map, or a corrupt baseline would authorize every
    pending path.
    """
    pending = record.get("guard_pending")
    if not isinstance(pending, dict):
        return None
    baseline = _guard_baseline(record)
    if baseline is None:
        return None
    keys = set(baseline) | set(pending)
    return sorted(k for k in keys if baseline.get(k) != pending.get(k))


def _guard_baseline(record: dict) -> dict | None:
    """The hook's accepted baseline map, else the first seen map, else None."""
    baseline = record.get("guard_baseline")
    # A present but non-dict baseline is untrusted evidence, not a fallback.
    if baseline is not None and not isinstance(baseline, dict):
        return None
    # An empty map is falsy: the hook falls back to the first seen map here too.
    if baseline:
        return baseline
    seen = record.get("guard_files_seen")
    if isinstance(seen, list) and seen and isinstance(seen[0], dict):
        return seen[0]
    return None


def _has_unsafe_value(record: dict, paths: list[str]) -> bool:
    """Whether a changed path's guard value is the 'missing' or 'irregular' sentinel."""
    pending = record.get("guard_pending") or {}
    return any(pending.get(p) in ("missing", "irregular") for p in paths)


def _safe_norm_paths(paths: list[str]) -> list[str] | None:
    # Normalize once; an unsafe path or a carve-out file makes the whole set
    # unauthorizable, so both checks run on one form.
    norm = [normalize(p) for p in paths]
    if any(p is None or _is_carveout(p) for p in norm):
        return None
    return norm


def _all_authorized(paths: list[str] | None, allowlist: list[str]) -> bool:
    if not paths:
        return False
    allowed = {normalize(a) for a in allowlist}
    allowed.discard(None)
    norm = _safe_norm_paths(paths)
    if not allowed or norm is None:
        return False
    return all(p in allowed for p in norm)


def _escalate(reason: str, notify: str) -> dict:
    return {"action": "escalate", "reply": None, "reason": reason, "notify": notify}


def _decide_awaiting(
    record: dict, allowlist: list[str], question: dict | None, notify_only: bool
) -> dict:
    # A concurrent hidden gate on a paused run (a pending approval, or a
    # transcript that cannot be read) is evidence to escalate, never to
    # auto-answer, so a guard pause can never mask another waiting question.
    outcome = question.get("outcome") if isinstance(question, dict) else None
    if outcome == "pending":
        return _escalate("hidden_gate", "an approval question is waiting on a paused run")
    if outcome in ("missing", "unreadable"):
        return _escalate("transcript_unobservable", "paused run, transcript unreadable")
    reason = record.get("paused_reason")
    if reason == "guard_changed":
        return _decide_guard(record, allowlist, notify_only)
    if reason in ("branch_mismatch", "no_message"):
        return _escalate(str(reason), f"paused: {reason}")
    return _escalate("needs_human", "a pause needs a human; qa-continue is deferred")


def _decide_guard(record: dict, allowlist: list[str], notify_only: bool) -> dict:
    paths = changed_guard_paths(record)
    if paths is None:
        return _escalate("guard_untrusted", "guard evidence cannot be trusted")
    if _has_unsafe_value(record, paths):
        return _escalate("guard_irregular", "a changed guard path is missing or not a regular file")
    if not notify_only and _all_authorized(paths, allowlist):
        return {
            "action": "auto_accept",
            "reply": "accept",
            "reason": "declared_loop_edit",
            "notify": None,
            "paths": paths,
            "authorization": "operator_launch_allowlist",
        }
    return _escalate("guard_changed", "loop files changed off the allowlist")


def _decide_running(record: dict, question: dict | None) -> dict:
    if not record.get("is_stale"):
        return {"action": "noop", "reply": None, "reason": "running", "notify": None}
    outcome = question.get("outcome") if isinstance(question, dict) else None
    if outcome == "pending":
        return _escalate("hidden_gate", "an approval question is waiting")
    if outcome in ("missing", "unreadable"):
        # A stale run whose transcript cannot be read may be sitting on a gate we
        # cannot see, so surface it rather than assume nothing is waiting.
        return _escalate("transcript_unobservable", "stale run, transcript unreadable")
    return {"action": "noop", "reply": None, "reason": "running_stale_no_gate", "notify": None}


def decide(
    record: dict,
    allowlist: list[str] | None = None,
    question: dict | None = None,
    notify_only: bool = False,
) -> dict:
    """Classify one run. See the module docstring for the policy."""
    allowlist = allowlist or []
    if not isinstance(record, dict):
        return _escalate("unobservable", "record is not an object")
    if record.get("condition") != "ok":
        return _escalate("unobservable", f"cannot read this run ({record.get('condition')})")
    if record.get("pending_promotion"):
        # The floor, checked before status: a promotion is never auto-answered,
        # whatever state the run reports.
        return _escalate("data_promotion", "promotion pending; the operator decides")
    if isinstance(question, dict) and question.get("outcome") == "permission_prompt":
        # A permission prompt escalates whatever the state file says, done
        # included. It is checked after the floors, so a floor keeps its own
        # reason, and before the status branches, so no status can hide it.
        tool = question.get("tool")
        command = question.get("command")
        return _escalate("permission_prompt", f"permission prompt on {tool}: {command}")
    status = record.get("status")
    if status == "failed":
        return _escalate("failed", "run failed; never auto-resume")
    if status == "done":
        return _escalate("done", "run finished")
    if status == "awaiting_human":
        return _decide_awaiting(record, allowlist, question, notify_only)
    return _decide_running(record, question)


def _load(path: str) -> object:
    """Load one JSON input; a bad or missing file reads as ``None``, not a crash."""
    try:
        if path == "-":
            return json.loads(sys.stdin.read() or "null")
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def _read_question(source: str | None) -> tuple[str, object]:
    """Return ``(status, value)`` where status is ``absent``, ``ok`` or ``malformed``."""
    # Empty or literal null is absent; a parse failure is malformed so the caller
    # rejects it, never reading a broken question as "no question" and answering.
    if source is None:
        return ("absent", None)
    try:
        raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ("malformed", None)
    raw = raw.strip()
    if not raw or raw == "null":
        return ("absent", None)
    try:
        return ("ok", json.loads(raw))
    except ValueError:
        return ("malformed", None)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Decide one run's supervisor action.")
    parser.add_argument("--record-file", required=True)
    # Default to no question rather than stdin, so a record-only call cannot hang
    # waiting on a terminal; pass "-" to read a question from stdin explicitly.
    parser.add_argument("--question-file", default=None)
    parser.add_argument("--allow-path", action="append", default=[])
    parser.add_argument("--notify-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    record = _load(args.record_file)
    if not isinstance(record, dict):
        print(
            f"MALFORMED_INPUT: --record-file did not parse to an object: {args.record_file}",
            file=sys.stderr,
        )
        return 2
    status_q, question = _read_question(args.question_file)
    if status_q == "malformed":
        print(
            f"MALFORMED_INPUT: --question-file did not parse: {args.question_file}", file=sys.stderr
        )
        return 2
    if question is not None and not isinstance(question, dict):
        print("MALFORMED_INPUT: --question-file must be a JSON object", file=sys.stderr)
        return 2
    decision = decide(record, args.allow_path, question, args.notify_only)
    print(json.dumps(decision, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
