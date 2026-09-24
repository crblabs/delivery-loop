"""Bind the v1 supervisor policy to the decision-table fixture.

Every row of ``supervisor-policy-matrix.json`` is one cell of the decision table.
The floor guard proves no row auto-answers a promotion, a failed run, or an
off-allowlist change, so the "never crossed" floor holds by construction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import supervisor_decide as sd

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "supervisor"
MATRIX = json.loads((FIXTURES / "supervisor-policy-matrix.json").read_text())
ROWS = MATRIX["rows"]


@pytest.mark.parametrize("row", ROWS, ids=[r["name"] for r in ROWS])
def test_decision_table_row(row):
    decision = sd.decide(row["record"], row["allowlist"], row["question"], row["notify_only"])
    assert decision["action"] == row["action"], row["name"]
    assert decision["reply"] == row["reply"], row["name"]
    assert decision["action"] in sd.ACTIONS


def test_floor_never_auto_answers_a_promotion():
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{".claude/settings.json": "a"}],
        "guard_pending": {".claude/settings.json": "b"},
        "pending_promotion": ["PROMOTION: data/budgets/spend.csv@abc"],
    }
    assert sd.decide(record, [".claude/settings.json"])["action"] == "escalate"


def test_notify_only_collapses_auto_to_escalate():
    # A non-carve-out loop path on the allowlist: notify-only escalates it,
    # notify-off auto-accepts it.
    loop_path = ".claude/skills/pipeline/stages/ship.md"
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{loop_path: "a"}],
        "guard_pending": {loop_path: "b"},
    }
    assert sd.decide(record, [loop_path], notify_only=True)["action"] == "escalate"
    assert sd.decide(record, [loop_path], notify_only=False)["action"] == "auto_accept"


def test_carveout_never_auto_accepts_even_when_allowlisted():
    # A carve-out enforcement file must escalate even if the operator allowlists
    # it, so the supervisor cannot bypass the boundary the hook enforces.
    carveout = ".claude/hooks/pipeline_stop.py"
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{carveout: "a"}],
        "guard_pending": {carveout: "b"},
    }
    assert sd.decide(record, [carveout], notify_only=False)["action"] == "escalate"


def test_auto_accept_output_carries_paths_and_authorization():
    loop_path = ".claude/skills/pipeline/stages/ship.md"
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{loop_path: "a"}],
        "guard_pending": {loop_path: "b"},
    }
    decision = sd.decide(record, [loop_path], notify_only=False)
    assert decision["action"] == "auto_accept"
    assert decision["paths"] == [loop_path]
    assert decision["authorization"] == "operator_launch_allowlist"


def test_guard_baseline_wins_over_seen_and_shows_a_deletion():
    # The decider diffs against guard_baseline, not the last seen map, and a
    # deletion (a path only in the baseline) is a change the operator must see.
    loop_path = ".claude/skills/pipeline/stages/ship.md"
    record = {
        "guard_baseline": {loop_path: "a", "other": "x"},
        "guard_files_seen": [{loop_path: "zzz"}],
        "guard_pending": {loop_path: "a"},
    }
    assert sd.changed_guard_paths(record) == ["other"]


@pytest.mark.parametrize("value", ["missing", "irregular"])
def test_irregular_or_missing_guard_value_escalates(value):
    loop_path = ".claude/skills/pipeline/stages/ship.md"
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{loop_path: "a"}],
        "guard_pending": {loop_path: value},
    }
    decision = sd.decide(record, [loop_path], notify_only=False)
    assert decision["action"] == "escalate"
    assert decision["reason"] == "guard_irregular"


def test_carveout_alias_path_escalates():
    # A non-canonical spelling of a carve-out file must still escalate: both the
    # carve-out and allowlist checks run on one normalized form.
    alias = ".claude/hooks/./pipeline_stop.py"
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{alias: "a"}],
        "guard_pending": {alias: "b"},
    }
    assert (
        sd.decide(record, [".claude/hooks/pipeline_stop.py"], notify_only=False)["action"]
        == "escalate"
    )
    assert sd.decide(record, [alias], notify_only=False)["action"] == "escalate"


def test_empty_guard_baseline_falls_back_to_first_seen():
    # An empty guard_baseline is falsy, so the decider falls back to the first
    # seen map, exactly as the hook does, and still sees a deletion.
    loop_path = ".claude/skills/pipeline/stages/ship.md"
    record = {
        "guard_baseline": {},
        "guard_files_seen": [{loop_path: "a", "other": "x"}],
        "guard_pending": {loop_path: "a"},
    }
    assert sd.changed_guard_paths(record) == ["other"]


def test_non_dict_guard_baseline_is_untrusted():
    record = {"guard_baseline": "not-a-map", "guard_pending": {"a": "1"}}
    assert sd.changed_guard_paths(record) is None


def test_cli_rejects_a_malformed_question(tmp_path):
    record = tmp_path / "record.json"
    record.write_text('{"condition": "ok", "status": "running", "is_stale": false}')
    bad = tmp_path / "q.json"
    bad.write_text("{ not json")
    assert sd.main(["--record-file", str(record), "--question-file", str(bad)]) == 2


def test_cli_rejects_a_non_object_question(tmp_path):
    record = tmp_path / "record.json"
    record.write_text('{"condition": "ok", "status": "running", "is_stale": false}')
    arr = tmp_path / "q.json"
    arr.write_text("[1, 2, 3]")
    assert sd.main(["--record-file", str(record), "--question-file", str(arr)]) == 2


def test_hidden_gate_on_a_paused_run_escalates_not_auto_accepts():
    # A pending approval on a paused guard run must escalate, never auto-answer.
    loop_path = ".claude/skills/pipeline/stages/ship.md"
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{loop_path: "a"}],
        "guard_pending": {loop_path: "b"},
    }
    question = {"outcome": "pending", "tool_use_id": "toolu_gate"}
    decision = sd.decide(record, [loop_path], question=question, notify_only=False)
    assert decision["action"] == "escalate"
    assert decision["reason"] == "hidden_gate"


def test_empty_allowlist_escalates_every_guard_change():
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_files_seen": [{".claude/settings.json": "a"}],
        "guard_pending": {".claude/settings.json": "b"},
    }
    assert sd.decide(record, [])["action"] == "escalate"


@pytest.mark.parametrize(
    "path,expected",
    [
        (".claude/settings.json", ".claude/settings.json"),
        ("./.claude/settings.json", ".claude/settings.json"),
        (".claude//settings.json", ".claude/settings.json"),
        ("../hooks/x.py", None),
        ("/abs/path", None),
        ("a/../b", None),
        ("", None),
        ("has\x00null", None),
    ],
)
def test_normalize(path, expected):
    assert sd.normalize(path) == expected


def test_changed_guard_paths_none_when_untrustworthy():
    # No pending map, or a missing or malformed accepted baseline: cannot trust
    # a diff, so return None and force the caller to escalate.
    assert sd.changed_guard_paths({"guard_pending": None}) is None
    assert sd.changed_guard_paths({}) is None
    assert sd.changed_guard_paths({"guard_pending": {"a": "1"}}) is None
    assert sd.changed_guard_paths({"guard_pending": {"a": "1"}, "guard_files_seen": []}) is None
    assert (
        sd.changed_guard_paths({"guard_pending": {"a": "1"}, "guard_files_seen": ["not-a-dict"]})
        is None
    )


def test_changed_guard_paths_diffs_against_last_accepted():
    record = {
        "guard_files_seen": [{"a": "1", "b": "1"}],
        "guard_pending": {"a": "1", "b": "2", "c": "9"},
    }
    assert sd.changed_guard_paths(record) == ["b", "c"]


def test_malformed_guard_baseline_escalates_not_auto_accepts():
    # A corrupt baseline must never authorize a change even if the pending path
    # is on the allowlist.
    record = {
        "condition": "ok",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "guard_pending": {".claude/settings.json": "b"},
        "guard_files_seen": [],
    }
    assert sd.decide(record, [".claude/settings.json"])["action"] == "escalate"


def test_promotion_escalates_even_while_running():
    record = {
        "condition": "ok",
        "status": "running",
        "is_stale": False,
        "pending_promotion": ["PROMOTION: data/budgets/spend.csv@abc"],
    }
    assert sd.decide(record)["action"] == "escalate"


def test_non_dict_record_escalates_not_crashes():
    assert sd.decide(None)["action"] == "escalate"
    assert sd.decide("nonsense")["action"] == "escalate"


@pytest.mark.parametrize("outcome", ["missing", "unreadable"])
def test_stale_run_with_unobservable_transcript_escalates(outcome):
    record = {"condition": "ok", "status": "running", "is_stale": True}
    assert sd.decide(record, question={"outcome": outcome})["action"] == "escalate"


@pytest.mark.parametrize("status", ["done", "failed", "running", "awaiting_human"])
def test_permission_prompt_escalates_whatever_the_status(status):
    record = {"condition": "ok", "status": status, "is_stale": False}
    question = {
        "outcome": "permission_prompt",
        "tool": "Bash",
        "command": "git push",
        "tool_use_id": "toolu_b",
    }
    decision = sd.decide(record, question=question)
    assert decision["action"] == "escalate"
    assert decision["reason"] == "permission_prompt"
    # The notification names the command so the operator can act on it.
    assert "git push" in decision["notify"]


def test_promotion_floor_keeps_its_reason_over_a_permission_prompt():
    # A promotion is checked before the prompt, so its reason wins; both escalate.
    record = {
        "condition": "ok",
        "status": "running",
        "pending_promotion": ["PROMOTION: data/budgets/spend.csv@abc"],
    }
    question = {"outcome": "permission_prompt", "tool": "Bash", "command": "x", "tool_use_id": "t"}
    decision = sd.decide(record, question=question)
    assert decision["action"] == "escalate"
    assert decision["reason"] == "data_promotion"
