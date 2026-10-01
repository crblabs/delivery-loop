"""The turn end: how a run advances, pauses, finishes, and notices a guarded edit."""

from __future__ import annotations

import json
from pathlib import Path

from core import guard
from core import pipeline_state as ps
from core import run_index as ri
from core import run_state as rs
from core import supervisor_decide as sd
from core import supervisor_scan as ss
from core import turn_end as te
from core.config import DEFAULTS, LoopConfig, StageSpec
from core.guard_evidence import guard_map

DONE = rs.DONE_TOKEN
CARD = "ASK   Which?\nREC   1. This\nALT   2. That\nREPLY 1 | 2\n" + rs.PAUSE_TOKEN


def _end(run: rs.Run, message: str, session: str = "s1"):
    return te.handle_turn_end(te.TurnEnd(session, message, "p1"), run)


def _state(run: rs.Run) -> dict:
    condition, state = rs.read(run)
    assert condition == "ok", condition
    return state


def _two_stages(**ship) -> LoopConfig:
    return LoopConfig(
        stages=(
            StageSpec(name="implement", clean_tree=True),
            StageSpec(name="ship", emits="PR: the pull request url", **ship),
        )
    )


def test_a_turn_without_a_token_is_reinjected_up_to_the_cap(start_run) -> None:
    # Value: protects=the loop pushes on unattended but reaches a person in bounded turns;
    # fails_when=the blocks stop before the cap, or never stop; why_new=eng E6; seam=none
    run, _ = start_run()
    for attempt in (1, 2):
        verdict = _end(run, "still thinking")
        assert verdict.block
        assert verdict.reinject.startswith("The stage is not finished")
        assert "Stage 1/5: autoplan" in verdict.reinject
        assert _state(run)["attempts"]["autoplan"] == attempt
    verdict = _end(run, "still thinking")
    assert not verdict.block and verdict.pause_reason == "no_message"
    state = _state(run)
    assert state["status"] == "awaiting_human"
    assert sd.decide({"condition": "ok", **state})["reason"] == "no_message"


def test_a_quoted_token_does_not_count(start_run) -> None:
    run, _ = start_run()
    verdict = _end(run, f"I will end with {DONE} later.\nNot yet.")
    assert verdict.block


def test_done_needs_the_value_the_stage_emits(start_run, tmp_path: Path) -> None:
    run, _ = start_run()
    assert _end(run, DONE).block
    assert "PLAN: the absolute path" in _end(run, DONE).reinject


def test_a_gated_stage_pauses_after_it_is_done(start_run, tmp_path: Path) -> None:
    run, _ = start_run()
    plan = tmp_path / "plan.md"
    plan.write_text("Plan.\n```loop-edits\nstages/qa.md\n```\n", encoding="utf-8")
    verdict = _end(run, f"Done.\nPLAN: {plan}\n{DONE}")
    assert verdict.pause_reason == "gate"
    state = _state(run)
    assert state["current_stage"] == "implement"
    assert state["plan_path"] == str(plan)
    assert state["declared_loop_edits"] == []
    assert state["pending_loop_edits"] == [".claude/skills/pipeline/stages/qa.md"]
    text = rs.resume(run, "person")
    assert text.startswith("Stage 2/5: implement")
    assert f"Approved plan: {plan}" in text
    assert _state(run)["declared_loop_edits"] == [".claude/skills/pipeline/stages/qa.md"]


def test_a_plan_unlocks_nothing_until_the_gate_is_approved(start_run, tmp_path) -> None:
    # Value: protects=a person approves the loop files a plan unlocks before the agent
    # can edit them, and approves the plan as it reads at resume; fails_when=the unlock
    # list is applied at the gate pause or kept from an earlier plan text;
    # why_new=red-team review; seam=none
    run, worktree = start_run()
    plan = tmp_path / "plan.md"
    plan.write_text("```loop-edits\nstages/qa.md\n```\n", encoding="utf-8")
    _end(run, f"PLAN: {plan}\n{DONE}")
    target = str(worktree / ".claude/skills/pipeline/stages/qa.md")
    call = guard.PreToolCall("s1", "edit", "Write", str(worktree), None, target)
    assert not guard.handle_pre_tool(call, run).allow
    plan.write_text("Revised: no loop files.\n", encoding="utf-8")
    rs.resume(run, "person")
    assert _state(run)["declared_loop_edits"] == []
    assert not guard.handle_pre_tool(call, run).allow


def test_a_pause_keeps_the_card_as_the_pending_question(start_run) -> None:
    run, _ = start_run()
    verdict = _end(run, "Context.\n" + CARD)
    assert verdict.pause_reason == "needs_human"
    state = _state(run)
    assert state["pending_question"].endswith(rs.PAUSE_TOKEN)
    assert state["paused_prompt_id"] == "p1"


def test_a_clean_tree_stage_needs_its_work_committed(make_repo, tmp_path: Path, git) -> None:
    worktree = make_repo(tmp_path / "host")
    run, _ = rs.start(worktree, "CRB-1", _two_stages(), "s1")
    (worktree / "new.py").write_text("x = 1\n", encoding="utf-8")
    verdict = _end(run, DONE)
    # An untracked file is named, with .gitignore offered for files that are not work.
    assert verdict.block and "untracked files (new.py)" in verdict.reinject
    assert ".gitignore" in verdict.reinject
    git(worktree, "add", "new.py")
    verdict = _end(run, DONE)
    assert verdict.block and "uncommitted changes; commit them" in verdict.reinject
    git(worktree, "commit", "-q", "-m", "add")
    verdict = _end(run, DONE)
    assert verdict.block and verdict.reinject.startswith("Stage 2/2: ship")


def test_a_clean_tree_stage_is_not_done_when_git_status_fails(make_repo, tmp_path) -> None:
    # Value: protects=a clean_tree stage fails closed when the tree cannot be checked;
    # fails_when=a failed git status reads as clean; why_new=only a clean and a dirty tree
    # are tested; seam=none
    worktree = make_repo(tmp_path / "host")
    run, _ = rs.start(worktree, "CRB-1", _two_stages(), "s1")
    (worktree / ".git" / "index").write_bytes(b"not an index")
    verdict = _end(run, DONE)
    assert verdict.block and "uncommitted changes" in verdict.reinject
    assert _state(run)["current_stage"] == "implement"


def test_a_clean_tree_check_ignores_an_inherited_git_dir(make_repo, tmp_path, monkeypatch) -> None:
    # Value: protects=clean_tree judges the run's own worktree; fails_when=git status
    # follows a GIT_DIR the session inherited to another, clean repository;
    # why_new=maintainability review; seam=none
    other = make_repo(tmp_path / "other")
    worktree = make_repo(tmp_path / "host")
    run, _ = rs.start(worktree, "CRB-1", _two_stages(), "s1")
    (worktree / "new.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    verdict = _end(run, DONE)
    assert verdict.block and "untracked files (new.py)" in verdict.reinject


def test_the_last_stage_finishes_the_run_and_drops_the_index(make_repo, tmp_path) -> None:
    worktree = make_repo(tmp_path / "host")
    run, _ = rs.start(worktree, "CRB-1", _two_stages(), "s1")
    _end(run, DONE)
    verdict = _end(run, f"PR: https://github.com/o/r/pull/7\n{DONE}")
    assert not verdict.block and verdict.pause_reason is None
    state = _state(run)
    assert state["status"] == "done"
    assert state["pr_url"] == "https://github.com/o/r/pull/7"
    assert ri.find_entry(str(worktree)) is None
    assert ri.read_entry(ri.session_path("s1")) is None


def test_another_session_in_the_worktree_is_left_alone(start_run) -> None:
    # Value: protects=a second terminal cannot drive or stall the run;
    # fails_when=any session's Stop advances it; why_new=session binding; seam=none
    run, _ = start_run()
    first = _end(run, "hello", session="other")
    assert not first.block and first.pause_reason is None
    state = _state(run)
    assert state["session_id"] == "s1" and state["attempts"]["autoplan"] == 0
    assert _end(run, "hello", session="other") == te.PASS
    assert _state(run)["revision"] == state["revision"]


def test_another_session_is_told_once_how_to_adopt_the_run(start_run) -> None:
    # Value: protects=after /clear the person learns why the run stopped being driven
    # and how to pick it up; fails_when=a new session id is ignored without a word;
    # why_new=red-team review; seam=none
    run, _ = start_run()
    note = _end(run, "hello", session="after-clear").note
    assert note and "/delivery-loop:pipeline resume" in note
    assert _end(run, "hello", session="after-clear").note is None


def test_an_unbound_run_binds_the_first_session_that_ends_a_turn(start_run) -> None:
    run, worktree = start_run(session=None)
    _end(run, "hello", session="s9")
    assert _state(run)["session_id"] == "s9"
    assert ri.read_entry(ri.session_path("s9"))["worktree"] == str(worktree)
    assert not _end(run, "hello", session="s1").block


def test_a_paused_run_lets_turns_end(start_run) -> None:
    run, _ = start_run()
    _end(run, CARD)
    assert _end(run, "anything") == te.PASS


def test_a_shell_edit_of_a_carve_out_pauses_the_run(start_run) -> None:
    # Value: protects=a shell write the edit guard cannot see still stops the run;
    # fails_when=the guard map misses loop.toml; why_new=guard evidence; seam=none
    run, worktree = start_run()
    (worktree / "loop.toml").write_text("[harness]\n", encoding="utf-8")
    verdict = _end(run, "editing")
    assert verdict.pause_reason == "guard_changed"
    state = _state(run)
    record = {"condition": "ok", **state}
    assert sd.changed_guard_paths(record) == ["loop.toml"]
    assert sd.decide(record)["reason"] == "guard_changed"
    assert "loop.toml" in state["pending_question"]


def test_a_guarded_edit_outranks_a_done_token(start_run, tmp_path) -> None:
    # Value: protects=an agent cannot shell-edit a carve-out and advance in the same turn;
    # fails_when=the token is read before the guard map is compared; why_new=every guard
    # test ends its turn without a token; seam=none
    run, worktree = start_run()
    plan = tmp_path / "plan.md"
    plan.write_text("plan", encoding="utf-8")
    (worktree / "loop.toml").write_text("[harness]\n", encoding="utf-8")
    verdict = _end(run, f"PLAN: {plan}\n{DONE}")
    assert verdict.pause_reason == "guard_changed"
    state = _state(run)
    assert state["current_stage"] == "autoplan"
    assert state["plan_path"] is None


def test_settings_that_switch_the_hooks_off_pause_the_run(start_run) -> None:
    run, worktree = start_run()
    (worktree / ".claude").mkdir()
    (worktree / ".claude/settings.local.json").write_text(
        json.dumps({"disableAllHooks": True}), encoding="utf-8"
    )
    assert _end(run, "x").pause_reason == "guard_changed"


def test_a_permission_rule_written_to_local_settings_does_not_pause(start_run) -> None:
    # Value: protects=runs are not paused each time a person answers a permission prompt;
    # fails_when=the settings file is hashed whole; why_new=key-hashed settings; seam=none
    run, worktree = start_run()
    settings = worktree / ".claude/settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}), encoding="utf-8")
    assert _end(run, "x").block
    settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(ls)", "Bash(git status)"]}}), encoding="utf-8"
    )
    assert _end(run, "x").block


def test_a_declared_loop_edit_becomes_the_new_baseline(start_run, tmp_path) -> None:
    run, worktree = start_run()
    plan = tmp_path / "plan.md"
    plan.write_text("```loop-edits\nstages/qa.md\n```\n", encoding="utf-8")
    _end(run, f"PLAN: {plan}\n{DONE}")
    rs.resume(run, "person")
    prompt = worktree / ".claude/skills/pipeline/stages/qa.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("House qa.", encoding="utf-8")
    assert _end(run, "working").block
    assert _state(run)["guard_baseline"][".claude/skills/pipeline/stages/qa.md"] != "missing"


def test_an_undeclared_loop_file_pauses(start_run) -> None:
    run, worktree = start_run()
    prompt = worktree / ".claude/skills/pipeline/stages/qa.md"
    prompt.parent.mkdir(parents=True)
    prompt.write_text("Sneaky.", encoding="utf-8")
    assert _end(run, "working").pause_reason == "guard_changed"


def test_creating_the_legacy_hooks_mid_run_pauses(start_run) -> None:
    run, worktree = start_run()
    hook = worktree / ".claude/hooks/pipeline_stop.py"
    hook.parent.mkdir(parents=True)
    hook.write_text("#", encoding="utf-8")
    assert _end(run, "x").pause_reason == "guard_changed"


def test_an_edited_config_snapshot_pauses(start_run) -> None:
    run, _ = start_run()
    snapshot = run.run_dir / "config.json"
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    data["carve_outs"] = [".git"]
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    run = rs.open_run(run.key, run.entry)
    assert _end(run, "x").pause_reason == "guard_changed"


def test_the_guard_map_leaves_out_the_git_directory(start_run) -> None:
    run, worktree = start_run()
    paths = guard_map(worktree, run.config, run.run_dir)
    assert not any(p.startswith(".git/") for p in paths)
    assert paths["loop.toml"] == "missing"
    assert paths[".claude/settings.json"] != "missing"


def test_every_write_reads_back_ok_for_the_scan(start_run, tmp_path) -> None:
    run, _ = start_run()
    plan = tmp_path / "p.md"
    plan.write_text("p", encoding="utf-8")
    for message in ("x", f"PLAN: {plan}\n{DONE}"):
        _end(run, message)
        state = _state(run)
        now = ps.parse_iso(state["updated_at"])
        records = ss.scan(None, now, 600, DEFAULTS, per_worktree=True)
        assert records[0]["condition"] == "ok"


def test_a_plan_that_is_not_a_small_regular_file_is_refused(start_run, tmp_path) -> None:
    # Value: protects=the Stop hook cannot be made to hang or exhaust memory, which would
    # let the turn end unchecked; fails_when=the PLAN path is read blindly; why_new=review
    # adversarial; seam=none
    import os

    run, _ = start_run()
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    for value in ("/dev/zero", str(fifo), str(tmp_path / "missing.md")):
        verdict = _end(run, f"PLAN: {value}\n{DONE}")
        assert verdict.block or verdict.pause_reason == "no_message"
    assert rs.read(run)[1]["current_stage"] == "autoplan"


def test_a_pr_line_that_is_not_a_url_does_not_finish_the_run(make_repo, tmp_path) -> None:
    worktree = make_repo(tmp_path / "host")
    run, _ = rs.start(worktree, "CRB-1", _two_stages(), "s1")
    _end(run, DONE)
    assert _end(run, f"PR: none\n{DONE}").block
    assert _state(run)["status"] == "running"


def test_a_deleted_declared_file_leaves_the_baseline(start_run, tmp_path) -> None:
    run, worktree = start_run()
    prompt = worktree / ".claude/skills/pipeline/stages/qa.md"
    plan = tmp_path / "plan.md"
    plan.write_text("```loop-edits\nstages/qa.md\n```\n", encoding="utf-8")
    _end(run, f"PLAN: {plan}\n{DONE}")
    rs.resume(run, "person")
    prompt.parent.mkdir(parents=True)
    prompt.write_text("House qa.", encoding="utf-8")
    assert _end(run, "x").block
    prompt.unlink()
    assert _end(run, "x").block
    assert ".claude/skills/pipeline/stages/qa.md" not in _state(run)["guard_baseline"]


def test_an_env_key_in_settings_pauses_the_run(start_run) -> None:
    # Value: protects=settings cannot repoint the hooks' python3 or state home unseen;
    # fails_when=env is left out of the hashed keys; why_new=review adversarial; seam=none
    run, worktree = start_run()
    (worktree / ".claude").mkdir()
    (worktree / ".claude/settings.local.json").write_text(
        json.dumps({"env": {"DELIVERY_LOOP_HOME": "/tmp/elsewhere"}}), encoding="utf-8"
    )
    assert _end(run, "x").pause_reason == "guard_changed"


def test_a_rewritten_git_config_pauses_the_run(start_run, git) -> None:
    run, worktree = start_run()
    git(worktree, "remote", "add", "origin", "https://example.com/o/r.git")
    assert _end(run, "x").pause_reason == "guard_changed"
    assert "<git>/config" in _state(run)["pending_question"]


def test_the_gate_card_shows_the_plan_and_what_it_unlocks(start_run, tmp_path) -> None:
    # Value: protects=a person approves the plan the run will act on; fails_when=the card
    # hides the file the agent named; why_new=re-review; seam=none
    run, _ = start_run()
    plan = tmp_path / "plan.md"
    plan.write_text("```loop-edits\nstages/qa.md\n```\n", encoding="utf-8")
    _end(run, f"PLAN: {plan}\n{DONE}")
    question = _state(run)["pending_question"]
    assert f"Plan read: {plan}" in question
    assert "Loop files it unlocks once approved: .claude/skills/pipeline/stages/qa.md" in question
    assert f"Plan: {plan}" in rs.status_text(run)
