"""The Claude Code adapter: payloads in, decisions out, and the error path."""

from __future__ import annotations

import json
from pathlib import Path

from adapters.claude_code import hooks as ch
from core import run_index as ri
from core import run_state as rs


def _stop(cwd: Path, message: str | None = "working", **extra) -> str:
    payload = {
        "session_id": "s1",
        "cwd": str(cwd),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "prompt_id": "p1",
    }
    if message is not None:
        payload["last_assistant_message"] = message
    return json.dumps({**payload, **extra})


def _pre(cwd: Path, tool: str, tool_input: dict, **extra) -> str:
    payload = {
        "session_id": "s1",
        "cwd": str(cwd),
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": tool_input,
    }
    return json.dumps({**payload, **extra})


def _decision(result: ch.HookResult) -> dict:
    return json.loads(result.stdout) if result.stdout else {}


def test_no_run_means_every_call_passes(tmp_path: Path) -> None:
    assert ch.run_stop(_stop(tmp_path)) == ch.PASS
    assert ch.run_guard(_pre(tmp_path, "Edit", {"file_path": "loop.toml"})) == ch.PASS


def test_a_stop_without_a_token_blocks_with_the_stage(start_run) -> None:
    _, worktree = start_run()
    out = _decision(ch.run_stop(_stop(worktree)))
    assert out["decision"] == "block"
    assert "Stage 1/5: autoplan" in out["reason"]


def test_the_guard_denies_in_the_harness_shape(start_run) -> None:
    _, worktree = start_run()
    out = _decision(ch.run_guard(_pre(worktree, "Write", {"file_path": "loop.toml"})))
    assert out["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "carve-out" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_an_allowed_call_prints_nothing(start_run) -> None:
    _, worktree = start_run()
    assert ch.run_guard(_pre(worktree, "Edit", {"file_path": "src/x.py"})) == ch.PASS
    assert ch.run_guard(_pre(worktree, "Bash", {"command": "ls"})) == ch.PASS


def test_the_agent_cannot_resume_through_bash_or_the_skill_tool(start_run, tmp_path) -> None:
    # Value: protects=a pause is cleared by a person only; fails_when=the agent runs the
    # slash command through the Skill tool; why_new=spike showed Skill bypasses
    # disable-model-invocation; seam=none
    _, worktree = start_run()
    for tool, tool_input in (
        ("Bash", {"command": "delivery-loop resume"}),
        ("Skill", {"skill": "delivery-loop:pipeline", "args": "resume"}),
        ("Skill", {"skill": "delivery-loop:pipeline", "args": "abort"}),
    ):
        out = _decision(ch.run_guard(_pre(worktree, tool, tool_input)))
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny", tool_input
    # Denied even where no run is active: there is nothing for the agent to resume.
    out = _decision(ch.run_guard(_pre(tmp_path, "Bash", {"command": "delivery-loop abort"})))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_agent_starting_a_run_leaves_its_session_as_intent(make_repo, tmp_path) -> None:
    worktree = make_repo(tmp_path / "host")
    ch.run_guard(_pre(worktree, "Bash", {"command": "cd . && delivery-loop start CRB-1"}))
    assert ri.take_intent(ri.worktree_key(str(worktree))) == "s1"


def test_a_payload_that_does_not_parse_passes_with_a_note(start_run) -> None:
    start_run()
    assert ch.run_stop("{not json").stderr
    assert ch.run_guard("[]").stderr
    assert not ch.run_stop("{not json").stdout


def test_an_unreadable_state_fails_closed_once(start_run) -> None:
    # Value: protects=a broken run is not silently unguarded, and the Stop block cannot loop;
    # fails_when=errors fail open with a run active, or block forever; why_new=error path;
    # seam=none
    run, worktree = start_run()
    run.state_path.write_text("{", encoding="utf-8")
    out = _decision(ch.run_stop(_stop(worktree)))
    assert out["decision"] == "block" and "abort" in out["reason"]
    again = ch.run_stop(_stop(worktree, stop_hook_active=True))
    assert not again.stdout and again.stderr
    deny = _decision(ch.run_guard(_pre(worktree, "Edit", {"file_path": "a.py"})))
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    # Both refusals are in the log the fix points the person to.
    events = ri.read_events(str(run.run_dir), 10)
    assert [e["hook"] for e in events if e.get("decision") == "error"] == ["stop", "stop", "guard"]


def test_an_unsupported_state_version_names_the_fix(start_run) -> None:
    run, worktree = start_run()
    state = json.loads(run.state_path.read_text(encoding="utf-8"))
    run.state_path.write_text(json.dumps({**state, "version": 99}), encoding="utf-8")
    out = _decision(ch.run_stop(_stop(worktree)))
    assert "another delivery-loop version" in out["reason"]


def test_a_stale_index_entry_is_removed_and_the_call_passes(start_run) -> None:
    run, worktree = start_run()
    run.state_path.unlink()
    result = ch.run_guard(_pre(worktree, "Edit", {"file_path": "loop.toml"}))
    assert result.stdout == "" and "stale" in result.stderr
    assert ri.find_entry(str(worktree)) is None


def test_the_message_falls_back_to_the_transcript_tail(start_run, tmp_path) -> None:
    _, worktree = start_run()
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"type": "user", "message": {"content": "go"}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ASK x\n"}]}},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "ASK y\n" + rs.PAUSE_TOKEN}]},
        },
        {"type": "system", "content": "noise"},
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    payload = _stop(worktree, message=None, transcript_path=str(transcript))
    assert (
        "paused this run (needs_human)" in json.loads(ch.run_stop(payload).stdout)["systemMessage"]
    )
    assert rs.read(rs.find(str(worktree)))[1]["paused_reason"] == "needs_human"


def test_only_the_transcript_tail_is_read(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ch, "TAIL_BYTES", 200)
    transcript = tmp_path / "t.jsonl"
    early = {"type": "assistant", "message": {"content": [{"type": "text", "text": "early"}]}}
    late = {"type": "assistant", "message": {"content": [{"type": "text", "text": "late"}]}}
    transcript.write_text(
        json.dumps(early) + "\n" + "x" * 5000 + "\n" + json.dumps(late) + "\n", encoding="utf-8"
    )
    assert ch.transcript_tail_message(transcript) == "late"


def test_every_decision_on_a_run_is_logged(start_run) -> None:
    run, worktree = start_run()
    ch.run_stop(_stop(worktree))
    ch.run_guard(_pre(worktree, "Write", {"file_path": "loop.toml"}))
    events = ri.read_events(str(run.run_dir), 10)
    assert [e["hook"] for e in events] == ["cli", "stop", "guard"]
    assert events[1]["decision"] == "block"
    assert events[2]["decision"] == "deny"


def test_a_session_bound_to_one_run_cannot_edit_another_runs_carve_out(start_run) -> None:
    # Value: protects=every run's rules apply to an edit in its worktree; fails_when=the
    # session's own run alone is judged; why_new=review adversarial; seam=none
    _, first = start_run("first", session="s1")
    _, second = start_run("second", session="s2")
    payload = _pre(first, "Write", {"file_path": str(second / "loop.toml")})
    out = _decision(ch.run_guard(payload))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_an_mcp_write_to_a_carve_out_is_denied(start_run) -> None:
    _, worktree = start_run()
    payload = _pre(worktree, "mcp__fs__write_file", {"path": str(worktree / "loop.toml")})
    assert _decision(ch.run_guard(payload))["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_tracker_write_is_denied_during_a_run_and_a_read_passes(start_run) -> None:
    # Value: protects=a run writes only to its worktree and its pull request, and an issue
    # goes through loop-issue; fails_when=a Linear MCP write lands mid-run; why_new=issue writer
    _, worktree = start_run()
    write = _pre(worktree, "mcp__linear-server__save_issue", {"title": "x", "team": "ENG"})
    out = _decision(ch.run_guard(write))["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert "loop-issue DRAFT.md" in out["permissionDecisionReason"]
    read = _pre(worktree, "mcp__linear-server__get_issue", {"id": "ENG-1"})
    assert ch.run_guard(read) == ch.PASS


def test_a_tracker_write_outside_a_run_is_left_to_the_person(tmp_path: Path) -> None:
    payload = _pre(tmp_path, "mcp__linear-server__save_issue", {"title": "x"})
    assert ch.run_guard(payload) == ch.PASS


def test_a_held_lock_fails_the_stop_closed_once_and_logs_it(start_run, monkeypatch) -> None:
    # Value: protects=a turn that cannot be recorded is not let through silently;
    # fails_when=the error branch passes or loops; why_new=review testing; seam=none
    import fcntl
    import os

    run, worktree = start_run()
    monkeypatch.setattr(ch, "STOP_LOCK_S", 0.1)
    fd = os.open(run.run_dir / ri.LOCK_FILE, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert _decision(ch.run_stop(_stop(worktree)))["decision"] == "block"
        assert not ch.run_stop(_stop(worktree, stop_hook_active=True)).stdout
    finally:
        os.close(fd)
    assert ri.read_events(str(run.run_dir), 1)[0]["decision"] == "error"


def test_an_unexpected_error_fails_the_guard_closed(start_run, monkeypatch) -> None:
    _, worktree = start_run()
    from core import guard

    def boom(call, run):
        raise KeyError("worktree")

    monkeypatch.setattr(guard, "handle_pre_tool", boom)
    out = _decision(ch.run_guard(_pre(worktree, "Edit", {"file_path": "a.py"})))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_transcript_fallback_never_reads_an_earlier_turn(tmp_path: Path) -> None:
    # Value: protects=a turn that ended on a tool call does not reuse the last turn's
    # token; fails_when=the fallback walks past the last user entry; why_new=review
    # adversarial; seam=none
    transcript = tmp_path / "t.jsonl"
    lines = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": rs.DONE_TOKEN}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}},
    ]
    transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    assert ch.transcript_tail_message(transcript) is None


def _prompt(cwd: Path, prompt: str, session: str = "s1") -> str:
    return json.dumps(
        {
            "session_id": session,
            "cwd": str(cwd),
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
        }
    )


def _context(result: ch.HookResult) -> str:
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_a_typed_resume_is_applied_by_the_prompt_hook(start_run) -> None:
    run, worktree = start_run()
    rs.update(run, lambda s: rs.pause(s, "needs_human", "ASK x"))
    text = _context(ch.run_prompt(_prompt(worktree, "/delivery-loop:pipeline resume")))
    assert text.startswith("delivery-loop resume: Stage 1/5")
    state = rs.read(run)[1]
    assert state["status"] == "running" and state["resumed_by"] == "person"


def test_a_typed_resume_from_a_new_session_adopts_the_run(start_run) -> None:
    run, worktree = start_run()
    _context(ch.run_prompt(_prompt(worktree, "/delivery-loop:pipeline resume", session="s9")))
    assert rs.read(run)[1]["session_id"] == "s9"


def test_a_typed_abort_ends_the_run(start_run) -> None:
    run, worktree = start_run()
    text = _context(ch.run_prompt(_prompt(worktree, "/delivery-loop:pipeline abort")))
    assert "Aborted" in text
    assert rs.read(run)[1]["status"] == "failed"


def test_a_typed_resume_that_cannot_apply_is_reported_and_changes_nothing(start_run) -> None:
    # Value: protects=a person typing resume on a running run is told why, and the run is
    # untouched; fails_when=the RunError escapes the prompt hook or a write happens;
    # why_new=only the successful prompt paths are tested; seam=none
    run, worktree = start_run()
    before = rs.read(run)[1]["revision"]
    text = _context(ch.run_prompt(_prompt(worktree, "/delivery-loop:pipeline resume")))
    assert "could not resume the run: nothing to resume" in text
    assert rs.read(run)[1]["revision"] == before


def test_other_prompts_pass_untouched(start_run) -> None:
    _, worktree = start_run()
    assert ch.run_prompt(_prompt(worktree, "keep going")) == ch.PASS
    assert ch.run_prompt("{bad") == ch.PASS


def test_a_crash_while_recording_an_intent_still_guards_the_run(start_run) -> None:
    # Value: protects=a planted directory cannot make the guard crash and fail open;
    # fails_when=the intent write raises before the guard runs; why_new=re-review; seam=none
    run, worktree = start_run()
    ri.abort_run(run.key, run.entry, "person")
    run, _ = start_run("second")
    intent = Path(ri.intent_path(ri.worktree_key(str(run.worktree))))
    (intent / "x").mkdir(parents=True)
    payload = _pre(
        run.worktree, "Bash", {"command": "git push --mirror origin; delivery-loop start x"}
    )
    out = _decision(ch.run_guard(payload))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_any_guard_error_on_a_run_denies(start_run, monkeypatch) -> None:
    _, worktree = start_run()
    monkeypatch.setattr(ch, "_guard", lambda payload: 1 / 0)
    out = _decision(ch.run_guard(_pre(worktree, "Bash", {"command": "ls"})))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_short_form_resume_does_not_touch_the_run(start_run) -> None:
    # Value: protects=another plugin's or a leftover /pipeline cannot resume this run;
    # fails_when=the short name is read as this plugin's command; why_new=adversarial
    # review; seam=none
    run, worktree = start_run()
    rs.update(run, lambda s: rs.pause(s, "needs_human", "ASK x"))
    assert ch.run_prompt(_prompt(worktree, "/pipeline resume")) == ch.PASS
    assert rs.read(run)[1]["status"] == "awaiting_human"


def test_a_pause_shows_the_person_its_card(start_run, tmp_path) -> None:
    # Value: protects=a person sees why the run stopped, and the plan a gate approves,
    # where they are; fails_when=the pause is silent; why_new=final re-review; seam=none
    _, worktree = start_run()
    plan = tmp_path / "plan.md"
    plan.write_text("plan", encoding="utf-8")
    out = json.loads(ch.run_stop(_stop(worktree, f"PLAN: {plan}\n{rs.DONE_TOKEN}")).stdout)
    assert out["systemMessage"].startswith("delivery-loop paused this run (gate).")
    assert f"Plan read: {plan}" in out["systemMessage"]
    assert "/delivery-loop:pipeline resume" in out["systemMessage"]


def test_a_new_session_in_the_worktree_is_shown_how_to_adopt_the_run(start_run) -> None:
    # Value: protects=the adoption note reaches the person as a system message; fails_when=
    # the Stop hook drops the verdict's note; why_new=red-team review; seam=none
    run, worktree = start_run()
    out = _decision(ch.run_stop(_stop(worktree, session_id="after-clear")))
    assert "/delivery-loop:pipeline resume" in out["systemMessage"]
    assert "decision" not in out


def test_a_move_is_judged_by_its_source_too(start_run, tmp_path) -> None:
    # Value: protects=a guarded file cannot be moved out of the worktree by an MCP tool;
    # fails_when=only the destination is checked; why_new=adversarial review; seam=none
    _, worktree = start_run()
    tool_input = {"source": str(worktree / "loop.toml"), "destination": str(tmp_path / "x")}
    out = _decision(ch.run_guard(_pre(tmp_path, "mcp__filesystem__move_file", tool_input)))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
