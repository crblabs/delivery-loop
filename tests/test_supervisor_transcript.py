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


def allow_attachment(tool_id="toolu_bash", decision="allow", hook_event="PreToolUse"):
    stdout = json.dumps({"hookSpecificOutput": {"permissionDecision": decision}})
    return {
        "type": "attachment",
        "attachment": {
            "type": "hook_success",
            "hookEvent": hook_event,
            "toolUseID": tool_id,
            "stdout": stdout,
        },
    }


def mode_line(mode, sidechain=False):
    line = {"type": "permission-mode", "permissionMode": mode}
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


# --- a call the guard allowed, or one under bypass, is not a prompt -----------

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "supervisor"
_ALLOWED_PUSH_TS = datetime(2026, 9, 19, 10, 19, 24, 472000, tzinfo=UTC)


def test_replay_real_allowed_push_is_none(tmp_path):
    # A committed real slice, frozen before the push completes. Aged
    # 40 minutes, far past the 60s threshold: none comes from the allow decision,
    # not from a young age or a completed result. This is the done-when replay.
    lines = [json.loads(x) for x in (FIXTURES / "allowed-push.jsonl").read_text().splitlines()]
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    (proj / f"{SID}.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    now = _ALLOWED_PUSH_TS + timedelta(minutes=40)
    assert st.read_question(tmp_path, SID, now)["outcome"] == "none"


def test_replay_control_without_allow_attachment_escalates(tmp_path):
    # The matched control: drop the allow attachment and the same aged push reads
    # as a genuine prompt, so the fixture proves the allow rule-out, not the age.
    lines = [json.loads(x) for x in (FIXTURES / "allowed-push.jsonl").read_text().splitlines()]
    lines = [x for x in lines if x.get("type") != "attachment"]
    write_transcript(tmp_path, "proj", SID, lines)
    now = _ALLOWED_PUSH_TS + timedelta(minutes=40)
    assert st.read_question(tmp_path, SID, now)["outcome"] == "permission_prompt"


def test_synthetic_allowed_push_beyond_threshold_is_none(tmp_path):
    lines = [bash(timestamp=_iso(NOW, 90)), allow_attachment("toolu_bash")]
    write_transcript(tmp_path, "proj", SID, lines)
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "none"


def test_bypass_mode_beyond_threshold_is_none(tmp_path):
    lines = [mode_line("bypassPermissions"), bash(timestamp=_iso(NOW, 90))]
    write_transcript(tmp_path, "proj", SID, lines)
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "none"


def test_ask_still_wins_under_bypass(tmp_path):
    # bypass does not answer an AskUserQuestion; catching it is the whole point.
    write_transcript(tmp_path, "proj", SID, [mode_line("bypassPermissions"), ask()])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "pending"


def test_allowed_later_call_does_not_hide_earlier_genuine_one(tmp_path):
    # One message, two unresolved calls; only the later is allowed. The earlier
    # genuine call must still escalate, so the rule-out is a per-candidate filter.
    line = {
        "type": "assistant",
        "timestamp": _iso(NOW, 90),
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_a",
                    "name": "Bash",
                    "input": {"command": "make db-reset"},
                },
                {
                    "type": "tool_use",
                    "id": "toolu_b",
                    "name": "Bash",
                    "input": {"command": "git push"},
                },
            ]
        },
    }
    write_transcript(tmp_path, "proj", SID, [line, allow_attachment("toolu_b")])
    out = st.read_question(tmp_path, SID, NOW)
    assert out["outcome"] == "permission_prompt"
    assert out["tool_use_id"] == "toolu_a"


def test_allow_for_a_different_id_does_not_silence_the_blocker(tmp_path):
    lines = [bash(tool_id="toolu_bash", timestamp=_iso(NOW, 90)), allow_attachment("toolu_other")]
    write_transcript(tmp_path, "proj", SID, lines)
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "permission_prompt"


@pytest.mark.parametrize(
    "att",
    [
        {
            "type": "attachment",
            "attachment": {"hookEvent": "PreToolUse", "toolUseID": "toolu_bash"},
        },
        {
            "type": "attachment",
            "attachment": {"hookEvent": "PreToolUse", "toolUseID": "toolu_bash", "stdout": ""},
        },
        {
            "type": "attachment",
            "attachment": {
                "hookEvent": "PreToolUse",
                "toolUseID": "toolu_bash",
                "stdout": "not json",
            },
        },
        {
            "type": "attachment",
            "attachment": {"hookEvent": "PreToolUse", "toolUseID": "toolu_bash", "stdout": "7"},
        },
        {
            "type": "attachment",
            "attachment": {
                "hookEvent": "PreToolUse",
                "toolUseID": "toolu_bash",
                "stdout": "[1, 2]",
            },
        },
        {
            "type": "attachment",
            "attachment": {
                "hookEvent": "PreToolUse",
                "toolUseID": "toolu_bash",
                "stdout": '{"hookSpecificOutput": 5}',
            },
        },
        {
            "type": "attachment",
            "attachment": {
                "hookEvent": "PreToolUse",
                "toolUseID": [],
                "stdout": '{"hookSpecificOutput": {"permissionDecision": "allow"}}',
            },
        },
        {"type": "attachment", "attachment": "not a dict"},
        allow_attachment("toolu_bash", decision="deny"),
        allow_attachment("toolu_bash", hook_event="PostToolUse"),
        {
            "type": "attachment",
            "attachment": {
                "hookEvent": "PreToolUse",
                "toolUseID": "toolu_bash",
                "stdout": '{"hookSpecificOutput": {"permissionDecision": "allow"}}',
            },
        },
        {
            "type": "attachment",
            "attachment": {
                "type": "hook_non_blocking_error",
                "hookEvent": "PreToolUse",
                "toolUseID": "toolu_bash",
                "stdout": '{"hookSpecificOutput": {"permissionDecision": "allow"}}',
            },
        },
    ],
)
def test_malformed_or_non_allow_attachment_still_escalates(tmp_path, att):
    # "Guard every access": a broken or non-allow attachment adds no id, so the
    # aged blocker still surfaces. A wrong event or a deny never suppresses it.
    write_transcript(tmp_path, "proj", SID, [bash(timestamp=_iso(NOW, 90)), att])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "permission_prompt"


def test_mode_reverts_to_default_before_the_call_escalates(tmp_path):
    # default -> bypass -> default: the call runs under default, so it escalates.
    lines = [
        mode_line("default"),
        mode_line("bypassPermissions"),
        mode_line("default"),
        bash(timestamp=_iso(NOW, 90)),
    ]
    write_transcript(tmp_path, "proj", SID, lines)
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "permission_prompt"


def test_sidechain_mode_line_does_not_suppress_a_main_call(tmp_path):
    # A subagent's bypass mode must not silence a later main-session prompt.
    lines = [mode_line("bypassPermissions", sidechain=True), bash(timestamp=_iso(NOW, 90))]
    write_transcript(tmp_path, "proj", SID, lines)
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "permission_prompt"


def test_later_bypass_does_not_retroactively_silence_an_earlier_call(tmp_path):
    # Mode is captured per call: a bypass line after the call cannot silence it.
    lines = [bash(timestamp=_iso(NOW, 90)), mode_line("bypassPermissions")]
    write_transcript(tmp_path, "proj", SID, lines)
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "permission_prompt"


def test_unknown_mode_before_first_call_escalates(tmp_path):
    # No mode line yet: mode is unknown, treated as not-bypass, so it escalates.
    write_transcript(tmp_path, "proj", SID, [bash(timestamp=_iso(NOW, 90))])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "permission_prompt"


# --- Unicode line boundaries inside a string do not hide the gate --------------


def _write_bytes_transcript(root: Path, session: str, lines) -> Path:
    # Byte-exact writer: json.dumps default ensure_ascii=True would escape U+0085
    # to a backslash escape, removing the literal that reproduces the bug.
    proj = root / "proj"
    proj.mkdir(parents=True, exist_ok=True)
    path = proj / f"{session}.jsonl"
    body = "\n".join(json.dumps(x, ensure_ascii=False) for x in lines) + "\n"
    path.write_bytes(body.encode("utf-8"))
    return path


def test_replay_nel_in_string_returns_pending(tmp_path):
    # The done-when replay: a committed slice with a literal U+0085 in a regex,
    # then the gate. splitlines() split the U+0085 line and made the file
    # unreadable; split("\n") reads it and returns the pending gate.
    raw = (FIXTURES / "nel-in-regex.jsonl").read_bytes()
    assert b"\xc2\x85" in raw, "fixture must carry a literal U+0085, not an escape"
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    (proj / f"{SID}.jsonl").write_bytes(raw)
    out = st.read_question(tmp_path, SID)
    assert out["outcome"] == "pending"
    assert out["tool_use_id"] == "toolu_gate"
    assert out["prompt"] == "Approve the plan?"


@pytest.mark.parametrize("codepoint", [0x0085, 0x2028, 0x2029])
def test_json_valid_line_boundary_in_string_still_returns_pending(tmp_path, codepoint):
    # splitlines() breaks on each of these; JSON allows them unescaped in a
    # string (only U+0000-U+001F are forbidden), so they belong in a real
    # transcript and must not hide the gate.
    q = {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [
                            {
                                "question": f"Approve{chr(codepoint)}now?",
                                "options": [{"label": "Yes"}],
                            }
                        ]
                    },
                }
            ]
        },
    }
    _write_bytes_transcript(tmp_path, SID, [q])
    assert st.read_question(tmp_path, SID)["outcome"] == "pending"


@pytest.mark.parametrize("codepoint", [0x000B, 0x000C])
def test_literal_control_char_in_string_stays_unreadable(tmp_path, codepoint):
    # splitlines() breaks on VT and FF too, but JSON forbids U+0000-U+001F
    # unescaped in a string, so a literal one is invalid JSON: json.loads
    # rejects it and the outcome stays unreadable, before and after the fix.
    proj = tmp_path / "proj"
    proj.mkdir(parents=True)
    line = '{"type":"assistant","x":"a' + chr(codepoint) + 'b"}'
    (proj / f"{SID}.jsonl").write_bytes((line + "\n").encode("utf-8"))
    assert st.read_question(tmp_path, SID)["outcome"] == "unreadable"


def test_detection_delay_and_decide_action(tmp_path):
    # The threshold is the detection delay for a genuine prompt: none below it,
    # permission_prompt above it, and supervisor_decide escalates the prompt.
    from core import supervisor_decide as sd

    write_transcript(tmp_path, "proj", SID, [bash(timestamp=_iso(NOW, 30))])
    assert st.read_question(tmp_path, SID, NOW)["outcome"] == "none"
    write_transcript(tmp_path, "proj", SID, [bash(timestamp=_iso(NOW, 90))])
    question = st.read_question(tmp_path, SID, NOW)
    assert question["outcome"] == "permission_prompt"
    record = {"condition": "ok", "status": "running", "is_stale": True}
    assert sd.decide(record, question=question)["action"] == "escalate"
