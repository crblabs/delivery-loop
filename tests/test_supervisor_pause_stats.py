"""Bind the pause classifier to its categories.

The reporter tells the operator how many pauses the auto-answer could remove
(``removable_auto_accept``) versus the ones it must never touch (a carve-out, an
off-allowlist edit, a promotion). Each category is exercised here.
"""

from __future__ import annotations

from core import supervisor_pause_stats as sps

LOOP = ".claude/skills/pipeline/stages/ship.md"
CARVEOUT = ".claude/hooks/pipeline_stop.py"


def _guard(pending_value, path=LOOP, baseline_value="aaa"):
    return {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{path: baseline_value}],
        "guard_pending": {path: pending_value},
    }


def test_removable_auto_accept():
    assert sps.classify_pause(_guard("bbb"), [LOOP]) == "removable_auto_accept"


def test_undeclared_off_allowlist():
    assert sps.classify_pause(_guard("bbb"), []) == "undeclared_off_allowlist"


def test_carveout_is_never_removable():
    record = _guard("bbb", path=CARVEOUT)
    assert sps.classify_pause(record, [CARVEOUT]) == "carveout"


def test_irregular_value():
    assert sps.classify_pause(_guard("irregular"), [LOOP]) == "guard_irregular"


def test_pending_promotion_is_never_removable():
    # An allowlisted guard pause that also has a pending promotion is a floor
    # pause, never removable, matching decide()'s data_promotion escalation.
    record = _guard("bbb")
    record["pending_promotion"] = ["PROMOTION: data/budgets/spend.csv@abc"]
    assert sps.classify_pause(record, [LOOP]) == "pause_promotion"


def test_untrusted_when_no_baseline():
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_pending": {LOOP: "bbb"},
        "guard_files_seen": [],
    }
    assert sps.classify_pause(record, [LOOP]) == "guard_untrusted"


def test_non_guard_pauses_and_states():
    assert (
        sps.classify_pause(
            {"condition": "ok", "status": "awaiting_human", "paused_reason": "needs_human"}, []
        )
        == "pause_needs_human"
    )
    assert (
        sps.classify_pause(
            {"condition": "ok", "status": "awaiting_human", "paused_reason": "branch_mismatch"}, []
        )
        == "pause_branch_mismatch"
    )
    assert sps.classify_pause({"condition": "ok", "status": "running"}, []) == "not_paused"
    assert sps.classify_pause({"condition": "corrupt"}, []) == "unobservable"
    assert sps.classify_pause("nonsense", []) == "unobservable"


def test_summarize_covers_every_category_key():
    records = [
        _guard("bbb"),
        _guard("bbb", path=CARVEOUT),
        {"condition": "ok", "status": "running"},
    ]
    summary = sps.summarize(records, [LOOP])
    assert set(summary) == set(sps.CATEGORIES)
    assert summary["removable_auto_accept"] == 1
    assert summary["carveout"] == 1
    assert summary["not_paused"] == 1
