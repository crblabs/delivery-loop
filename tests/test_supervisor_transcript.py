"""The transcript reader: five outcomes, no false positives."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from adapters.claude_code import supervisor_transcript as st

SID = "session-xyz"
NOW = datetime(2026, 9, 18, 14, 30, 0, tzinfo=UTC)


def _iso(now: datetime, ago_seconds: float) -> str:
    return (now - timedelta(seconds=ago_seconds)).isoformat().replace("+00:00", "Z")


def ask(tool_id="toolu_1"):
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [
                            {
                                "question": "Approve the plan?",
                                "options": [{"label": "Yes (recommended)"}, {"label": "No"}],
                            }
                        ]
                    },
                }
            ]
        },
    }


def result(tool_id="toolu_1"):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id}]},
    }


def other_tool(tool_id="toolu_bash"):
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {}}]},
    }


def bash(tool_id="toolu_bash", timestamp=None, command="git push", sidechain=False):
    line = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": "Bash",
                    "input": {"command": command},
                }
            ]
        },
    }
    if timestamp is not None:
        line["timestamp"] = timestamp
    if sidechain:
        line["isSidechain"] = True
    return line


def write_transcript(root: Path, project: str, session: str, lines) -> Path:
    proj = root / project
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    return path


def test_pending(tmp_path):
    write_transcript(tmp_path, "proj", SID, [ask()])
    out = st.read_question(tmp_path, SID)
    assert out["outcome"] == "pending"
    assert out["tool_use_id"] == "toolu_1"
    assert out["prompt"] == "Approve the plan?"
    recommended = [o["label"] for o in out["options"] if o["recommended"]]
    assert recommended == ["Yes (recommended)"]


def test_none_when_resolved(tmp_path):
    write_transcript(tmp_path, "proj", SID, [ask(), result()])
    assert st.read_question(tmp_path, SID)["outcome"] == "none"


def test_none_when_pending_tool_has_no_timestamp(tmp_path):
    # A pending Bash with no line timestamp cannot be aged, so it stays none.
    write_transcript(tmp_path, "proj", SID, [other_tool()])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "none"


def test_none_when_trailing_tool_has_no_timestamp(tmp_path):
    # A resolved question then an unresolved, untimestamped Bash is still none.
    write_transcript(tmp_path, "proj", SID, [ask(), result(), other_tool()])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "none"


def test_permission_prompt_when_bash_older_than_sixty_seconds(tmp_path):
    write_transcript(tmp_path, "proj", SID, [bash(timestamp=_iso(NOW, 90))])
    out = st.read_question(tmp_path, SID, NOW)
    assert out["outcome"] == "permission_prompt"
    assert out["tool"] == "Bash"
    assert out["command"] == "git push"
    assert out["tool_use_id"] == "toolu_bash"


def test_none_when_bash_inside_sixty_seconds(tmp_path):
    write_transcript(tmp_path, "proj", SID, [bash(timestamp=_iso(NOW, 30))])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "none"


def test_exact_sixty_second_boundary_is_none(tmp_path):
    # "Older than 60 seconds" is strict, so exactly 60 seconds is not yet a prompt.
    write_transcript(tmp_path, "proj", SID, [bash(timestamp=_iso(NOW, 60))])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "none"


def test_future_timestamp_is_none(tmp_path):
    write_transcript(tmp_path, "proj", SID, [bash(timestamp=_iso(NOW, -120))])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "none"


def test_sidechain_bash_is_ignored(tmp_path):
    # A subagent's own in-flight tool must not read as a main-session prompt.
    write_transcript(tmp_path, "proj", SID, [bash(timestamp=_iso(NOW, 90), sidechain=True)])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "none"


def test_pending_question_wins_over_a_later_permission_prompt(tmp_path):
    # An unresolved question outranks a later, old, unresolved Bash.
    write_transcript(
        tmp_path, "proj", SID, [ask(), bash(tool_id="toolu_b", timestamp=_iso(NOW, 90))]
    )
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "pending"


def test_resolved_bash_before_pending_bash_uses_the_pending_one(tmp_path):
    lines = [
        bash(tool_id="toolu_a", timestamp=_iso(NOW, 300), command="echo done"),
        result("toolu_a"),
        bash(tool_id="toolu_b", timestamp=_iso(NOW, 90), command="git push"),
    ]
    write_transcript(tmp_path, "proj", SID, lines)
    out = st.read_question(tmp_path, SID, NOW)
    assert out["outcome"] == "permission_prompt"
    assert out["tool_use_id"] == "toolu_b"
    assert out["command"] == "git push"


@pytest.mark.parametrize("bad_id", [[], {}, None, 7])
def test_non_string_tool_id_does_not_crash(tmp_path, bad_id):
    # An unhashable id ([] or {}) once crashed the pending set test.
    line = {
        "timestamp": _iso(NOW, 90),
        "message": {"content": [{"type": "tool_use", "id": bad_id, "name": "Bash", "input": {}}]},
    }
    write_transcript(tmp_path, "proj", SID, [line])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "none"


def test_malformed_tool_does_not_hide_a_real_question(tmp_path):
    # A malformed unrelated tool must not block detection of a valid question.
    bad = {"message": {"content": [{"type": "tool_use", "id": [], "name": "Bash", "input": {}}]}}
    write_transcript(tmp_path, "proj", SID, [ask(), bad])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "pending"


def test_missing(tmp_path):
    assert st.read_question(tmp_path, "no-such-session")["outcome"] == "missing"


def test_truncated_last_line_is_unreadable(tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    path = proj / f"{SID}.jsonl"
    path.write_text(json.dumps(ask()) + "\n" + '{"type":"assis', encoding="utf-8")
    assert st.read_question(tmp_path, SID)["outcome"] == "unreadable"


def test_ambiguous_duplicate_session(tmp_path):
    write_transcript(tmp_path, "projA", SID, [ask()])
    write_transcript(tmp_path, "projB", SID, [ask()])
    assert st.read_question(tmp_path, SID)["outcome"] == "unreadable"


def test_ambiguous_when_second_copy_is_nested(tmp_path):
    write_transcript(tmp_path, "proj", SID, [ask()])
    write_transcript(tmp_path, "proj/nested", SID, [ask()])
    assert st.read_question(tmp_path, SID)["outcome"] == "unreadable"


@pytest.mark.parametrize("bad", ["/tmp/victim", "../../victim", "wild*card", "a/b", "", ".."])
def test_invalid_session_id_is_unreadable(tmp_path, bad):
    assert st.read_question(tmp_path, bad)["outcome"] == "unreadable"


@pytest.mark.parametrize(
    "line",
    [
        None,
        {"message": None},
        {"message": {"content": [{"type": "tool_result", "tool_use_id": []}]}},
        {
            "message": {
                "content": [
                    {"type": "tool_use", "name": "AskUserQuestion", "id": "t", "input": None}
                ]
            }
        },
    ],
)
def test_malformed_shapes_do_not_crash(tmp_path, line):
    write_transcript(tmp_path, "proj", SID, [line])
    out = st.read_question(tmp_path, SID)
    assert out["outcome"] in ("none", "pending", "unreadable")


def test_null_label_option_does_not_crash(tmp_path):
    block = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "t",
                    "name": "AskUserQuestion",
                    "input": {"questions": [{"question": "q?", "options": [{"label": None}]}]},
                }
            ]
        },
    }
    write_transcript(tmp_path, "proj", SID, [block])
    out = st.read_question(tmp_path, SID)
    assert out["outcome"] == "pending"
    assert out["options"] == [{"label": "", "recommended": False}]
