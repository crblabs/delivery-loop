"""The turn end: how a run advances, pauses, finishes, and notices a guarded edit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    # can edit them, and exactly the ones the card listed; fails_when=the unlock list is
    # applied at the gate pause, or read again from a plan the agent edited since;
    # why_new=red-team review; seam=none
    run, worktree = start_run()
    plan = tmp_path / "plan.md"
    plan.write_text("```loop-edits\nstages/qa.md\n```\n", encoding="utf-8")
    _end(run, f"PLAN: {plan}\n{DONE}")
    qa = str(worktree / ".claude/skills/pipeline/stages/qa.md")
    review = str(worktree / ".claude/skills/pipeline/stages/review.md")

    def write(path: str) -> bool:
        call = guard.PreToolCall("s1", "edit", "Write", str(worktree), None, path)
        return guard.handle_pre_tool(call, run).allow

    assert not write(qa)
    plan.write_text("```loop-edits\nstages/qa.md\nstages/review.md\n```\n", encoding="utf-8")
    rs.resume(run, "person")
    assert write(qa) and not write(review)


def test_a_later_gate_does_not_read_the_plan_again(start_run, tmp_path) -> None:
    # Value: protects=approving the review gate unlocks nothing new; fails_when=every gate
    # resume re-reads a plan file the agent can edit; why_new=cycle-2 review; seam=none
    run, worktree = start_run()
    plan = tmp_path / "plan.md"
    plan.write_text("No loop files.\n", encoding="utf-8")
    _end(run, f"PLAN: {plan}\n{DONE}")
    rs.resume(run, "person")
    plan.write_text("```loop-edits\nstages/qa.md\n```\n", encoding="utf-8")
    while _state(run)["status"] == "running":
        _end(run, DONE)
    assert _state(run)["paused_reason"] == "gate"
    rs.resume(run, "person")
    assert _state(run)["declared_loop_edits"] == []


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


def test_a_busy_lock_never_blocks_a_session_that_does_not_drive_the_run(
    start_run, monkeypatch
) -> None:
    # Value: protects=another session's turn is never blocked by the adoption note;
    # fails_when=a held lock on the note's write fails the bystander closed;
    # why_new=cycle-2 review; seam=none
    run, _ = start_run()

    def busy(*args, **kwargs):
        raise ri.LockTimeout("held")

    monkeypatch.setattr(rs, "update", busy)
    assert _end(run, "hello", session="other") == te.PASS


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


def test_a_declared_directory_never_accepts_a_carve_out(make_repo, tmp_path) -> None:
    # Value: protects=a plan that declares .claude/ cannot slip a settings change past the
    # turn end; fails_when=a declared ancestor directory accepts the carve-out as the new
    # baseline; why_new=adversarial review; seam=none
    worktree = make_repo(tmp_path / "host")
    config = LoopConfig(
        stages=(StageSpec(name="implement"), StageSpec(name="ship")),
        loop_prefixes=(".claude/",),
    )
    run, _ = rs.start(worktree, "CRB-1", config, "s1")
    rs.update(run, lambda s: s.__setitem__("declared_loop_edits", [".claude/"]))
    (worktree / ".claude").mkdir(exist_ok=True)
    (worktree / ".claude/settings.json").write_text('{"disableAllHooks": true}', "utf-8")
    assert _end(run, "x").pause_reason == "guard_changed"


@pytest.mark.parametrize(
    ("where", "text"),
    [
        ("user-gitconfig", '[url "https://elsewhere/"]\n\tpushInsteadOf = https://github.com/\n'),
        ("user-settings", '{"enabledPlugins": {"delivery-loop@crblabs": false}}'),
        ("worktree-config", '[remote "origin"]\n\turl = https://elsewhere/r.git\n'),
    ],
)
def test_a_shell_write_outside_the_worktree_that_redirects_the_run_pauses_it(
    start_run, where: str, text: str
) -> None:
    # Value: protects=a push redirect or a plugin switch-off made from the shell is seen at
    # the next turn end; fails_when=only the repository's own config and settings are
    # hashed; why_new=adversarial review; seam=none
    run, worktree = start_run()
    home = Path.home()
    path = {
        "user-gitconfig": home / ".gitconfig",
        "user-settings": home / ".claude/settings.json",
        "worktree-config": worktree / ".git/config.worktree",
    }[where]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    assert _end(run, "x").pause_reason == "guard_changed"


def test_another_plugin_in_the_user_settings_does_not_pause_the_run(start_run) -> None:
    run, _ = start_run()
    settings = Path.home() / ".claude/settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text('{"enabledPlugins": {"other@x": true}}', encoding="utf-8")
    assert _end(run, "x").pause_reason is None


EM_DASH = chr(0x2014)


def test_a_clean_tree_stage_is_not_done_while_the_branch_adds_a_dash(
    make_repo, tmp_path: Path, git
) -> None:
    # Value: protects=a run cannot finish a stage with an em dash in its work;
    # fails_when=the hook advances over a dash; why_new=the stages only asked; seam=none
    worktree = make_repo(tmp_path / "host")
    run, _ = rs.start(worktree, "CRB-1", _two_stages(), "s1")
    (worktree / "notes.md").write_text(f"one {EM_DASH} two\n", encoding="utf-8")
    git(worktree, "add", "notes.md")
    git(worktree, "commit", "-q", "-m", "notes")
    verdict = _end(run, DONE)
    assert verdict.block and "banned dash" in verdict.reinject
    assert "notes.md:1:5" in verdict.reinject
    assert _state(run)["current_stage"] == "implement"
    (worktree / "notes.md").write_text("one, two\n", encoding="utf-8")
    git(worktree, "commit", "-q", "-am", "fix the dash")
    verdict = _end(run, DONE)
    assert verdict.block and verdict.reinject.startswith("Stage 2/2: ship")


def test_a_dash_in_a_commit_message_blocks_the_stage(make_repo, tmp_path: Path, git) -> None:
    worktree = make_repo(tmp_path / "host")
    run, _ = rs.start(worktree, "CRB-1", _two_stages(), "s1")
    (worktree / "a.md").write_text("fine\n", encoding="utf-8")
    git(worktree, "add", "a.md")
    git(worktree, "commit", "-q", "-m", f"add a {EM_DASH} file")
    verdict = _end(run, DONE)
    assert verdict.block and "message:1:7" in verdict.reinject


def test_a_dash_from_before_the_run_is_not_the_run_s_to_fix(make_repo, tmp_path: Path, git) -> None:
    worktree = make_repo(tmp_path / "host")
    (worktree / "old.md").write_text(f"legacy {EM_DASH}\n", encoding="utf-8")
    git(worktree, "add", "old.md")
    git(worktree, "commit", "-q", "-m", f"legacy {EM_DASH}")
    run, _ = rs.start(worktree, "CRB-1", _two_stages(), "s1")
    assert _state(run)["start_head"] == git(worktree, "rev-parse", "HEAD").strip()
    verdict = _end(run, DONE)
    assert verdict.block and verdict.reinject.startswith("Stage 2/2: ship")


def test_the_dash_base_is_origin_head_when_the_repository_has_one(
    make_repo, tmp_path: Path, git
) -> None:
    origin = make_repo(tmp_path / "origin")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    assert te._dash_base(clone, {"start_head": "abc"}) == "origin/HEAD"
    assert te._dash_base(origin, {"start_head": "abc"}) == "abc"
    assert te._dash_base(origin, {}) is None


# --- Waiting on background tasks ----------------------------------------------


def _wait(run: rs.Run, message: str, tasks: tuple[str, ...], session: str = "s1"):
    return te.handle_turn_end(te.TurnEnd(session, message, "p1", tasks), run)


def test_a_turn_that_waits_on_a_background_task_ends_without_an_attempt(start_run) -> None:
    # Value: protects=the CRB-28 acceptance: an obedient wait is neither refused nor
    # counted; fails_when=a no-token turn with a running task is blocked or counted;
    # why_new=CRB-28; seam=none
    run, _ = start_run()
    verdict = _wait(run, "Waiting for the spec reviewer.", ("a1",))
    assert verdict == te.TurnEndVerdict(block=False, waiting=True, tasks=("a1",))
    state = _state(run)
    assert state["attempts"]["autoplan"] == 0 and state["total_attempts"] == 0
    assert state["waiting_on"] == ["a1"] and state["waiting_since"] is not None
    assert state["wait_turns"]["autoplan"] == 1
    assert state["history"][-1] | {"at": None} == {
        "at": None,
        "event": "wait",
        "stage": "autoplan",
        "tasks": ["a1"],
    }


def test_the_wait_clears_when_the_task_reports(start_run) -> None:
    run, _ = start_run()
    _wait(run, "Waiting.", ("a1",))
    verdict = _wait(run, "still thinking", ())
    assert verdict.block and verdict.reinject.startswith("The stage is not finished")
    state = _state(run)
    assert state["waiting_on"] == [] and state["waiting_since"] is None


def test_done_is_refused_while_a_background_task_runs(start_run, tmp_path) -> None:
    # Value: protects=the next stage never starts beside a running reviewer; fails_when=
    # STAGE DONE advances with a task pending; why_new=CRB-28; seam=none
    run, _ = start_run()
    plan = tmp_path / "plan.md"
    plan.write_text("Plan.\n", encoding="utf-8")
    verdict = _wait(run, f"PLAN: {plan}\n{DONE}", ("b1",))
    assert verdict.block
    assert "a background task is still running (b1)" in verdict.reinject
    assert DEFAULTS.stop_task_tool in verdict.reinject
    state = _state(run)
    assert state["current_stage"] == "autoplan" and state["attempts"]["autoplan"] == 1


def test_a_refused_stage_done_at_the_attempt_cap_never_releases_a_running_task(start_run) -> None:
    # Value: protects=review red team: one early STAGE DONE after two misses must not let a
    # resume release a reviewer that still works, so the stage advances beside it;
    # fails_when=_no_token sets wait_capped; why_new=review; seam=none
    run, _ = start_run()
    for _ in range(2):
        assert _end(run, "thinking").block
    assert _wait(run, "Waiting.", ("a1", "a2")).waiting
    verdict = _wait(run, DONE, ("a2",))
    assert verdict.pause_reason == "no_message"
    state = _state(run)
    assert not state.get("wait_capped")
    card = state["pending_question"]
    assert "turns that were not accepted (last: a background task is still running" in card
    rs.resume(run, "person")
    state = _state(run)
    assert state["released_tasks"] == [] and state["waiting_on"] == ["a2"]
    assert _wait(run, DONE, ("a2",)).block


def test_the_wait_cap_pauses_with_a_card_naming_the_tasks(start_run) -> None:
    # Value: protects=the interim wait limit and its card; fails_when=waits never stop or
    # stop early; why_new=CRB-28; seam=none
    run, _ = start_run()
    for _ in range(rs.MAX_WAITS):
        assert _wait(run, "Waiting.", ("a1", "b2")).waiting
    verdict = _wait(run, "Waiting.", ("a1", "b2"))
    assert verdict.pause_reason == "no_message" and not verdict.waiting
    state = _state(run)
    assert state["wait_capped"] is True
    assert state["attempts"]["autoplan"] == 0 and state["total_attempts"] == 0
    card = state["pending_question"]
    assert f"waited {rs.MAX_WAITS} turns" in card and "(a1, b2, since" in card
    assert DEFAULTS.resume_command in card and DEFAULTS.abort_command in card


def test_waiting_since_runs_from_the_start_of_a_wait_until_nothing_is_pending(start_run) -> None:
    # Value: protects=a task that never reports cannot hide behind newer ones from the
    # supervisor's stale wait; fails_when=a new task restarts the clock; why_new=review
    # adversarial; seam=none
    run, _ = start_run()
    _wait(run, "Waiting.", ("a1", "b2"))
    first = _state(run)["waiting_since"]
    _wait(run, "Waiting.", ("a1", "c3"))
    assert _state(run)["waiting_since"] == first
    _end(run, "thinking")
    assert _state(run)["waiting_since"] is None


def test_the_wait_count_restarts_when_nothing_is_pending(start_run) -> None:
    # Value: protects=review red team: the cap bounds one wait, so a stage with several review
    # rounds is not paused while healthy; fails_when=wait_turns counts the whole stage;
    # why_new=review; seam=none
    run, _ = start_run()
    for _ in range(rs.MAX_WAITS):
        assert _wait(run, "Waiting.", ("a1",)).waiting
    assert _end(run, "Reviewed; next round.").block
    assert _state(run)["wait_turns"]["autoplan"] == 0
    for _ in range(rs.MAX_WAITS):
        assert _wait(run, "Waiting.", ("b1",)).waiting


def test_a_guard_pause_records_what_the_run_waits_on(start_run) -> None:
    # Value: protects=the waiting fields are current whatever the verdict; fails_when=the
    # record moves after the guard check; why_new=review plan audit; seam=none
    run, worktree = start_run()
    _end(run, "thinking")
    (worktree / "loop.toml").write_text("# changed\n", encoding="utf-8")
    verdict = _wait(run, "Waiting.", ("a1",))
    assert verdict.pause_reason == "guard_changed"
    assert _state(run)["waiting_on"] == ["a1"]


def test_a_pause_still_records_what_the_run_waits_on(start_run) -> None:
    run, _ = start_run()
    verdict = _wait(run, CARD, ("a1",))
    assert verdict.pause_reason == "needs_human"
    assert _state(run)["waiting_on"] == ["a1"]


def test_released_tasks_are_not_waited_on_again(start_run) -> None:
    run, _ = start_run()
    _wait(run, "Waiting.", ("a1",))
    rs.resume(run, "person", "s1")
    assert _state(run)["released_tasks"] == ["a1"]
    assert _wait(run, "still thinking", ("a1",)).block


def test_the_wait_count_restarts_in_the_next_stage(start_run, tmp_path) -> None:
    run, _ = start_run()
    _wait(run, "Waiting.", ("a1",))
    plan = tmp_path / "plan.md"
    plan.write_text("Plan.\n", encoding="utf-8")
    assert _wait(run, f"PLAN: {plan}\n{DONE}", ()).pause_reason == "gate"
    assert _state(run)["wait_turns"]["implement"] == 0


def test_a_run_started_before_the_wait_fields_can_still_wait(start_run) -> None:
    # Value: protects=runs in flight when the plugin updates; fails_when=the rules index a
    # field an old state lacks; why_new=CRB-28 CEO Sec 9; seam=none
    run, _ = start_run()

    def strip(state: dict) -> None:
        for key in ("waiting_on", "waiting_since", "wait_turns", "wait_capped", "released_tasks"):
            state.pop(key)
        state["caps"] = {"attempts": rs.MAX_ATTEMPTS}

    rs.update(run, strip)
    assert _wait(run, "Waiting.", ("a1",)).waiting
    assert _state(run)["wait_turns"] == {"autoplan": 1}


def test_a_wait_on_shell_commands_alone_is_marked_for_the_faster_alarm(start_run) -> None:
    # Value: protects=review D5: a dev server's wait reaches a person sooner; fails_when=
    # the shell-only flag is lost or set beside an agent; why_new=review; seam=none
    run, _ = start_run()
    te.handle_turn_end(te.TurnEnd("s1", "Waiting.", "p1", ("b1",), ("b1",)), run)
    assert _state(run)["waiting_shell_only"] is True
    te.handle_turn_end(te.TurnEnd("s1", "Waiting.", "p1", ("a1", "b1"), ("b1",)), run)
    assert _state(run)["waiting_shell_only"] is False
    _end(run, "thinking")
    assert _state(run)["waiting_shell_only"] is False


def test_an_unread_task_list_refuses_stage_done_and_keeps_the_wait(start_run) -> None:
    # Value: protects=review D11: a transcript the adapter could not read never lets the
    # stage advance beside a reviewer, nor resets the wait clock; fails_when=a failed scan
    # reads as no task; why_new=review red team; seam=none
    run, _ = start_run()
    assert _wait(run, "Waiting.", ("a1",)).waiting
    since = _state(run)["waiting_since"]
    unknown = te.TurnEnd("s1", DONE, "p1", (), (), tasks_unknown=True)
    verdict = te.handle_turn_end(unknown, run)
    assert verdict.block and "could not read which background tasks" in verdict.reinject
    state = _state(run)
    assert state["waiting_on"] == ["a1"] and state["waiting_since"] == since
    assert state["attempts"]["autoplan"] == 1


def test_the_attempts_pause_says_how_to_release_a_task_still_waited_on(start_run) -> None:
    # Value: protects=review adversarial: a task whose notification was lost can still be
    # released after an attempts pause; fails_when=the card hides the second resume;
    # why_new=review; seam=none
    run, _ = start_run()
    for _ in range(3):
        _wait(run, DONE, ("a1",))
    assert "a1; if they will never report" in _state(run)["pending_question"]
    rs.resume(run, "person")
    rs.resume(run, "person")
    assert _state(run)["released_tasks"] == ["a1"]


def test_a_turn_end_records_the_session_s_activity_file(start_run) -> None:
    run, _ = start_run()
    te.handle_turn_end(te.TurnEnd("s1", "x", "p1", activity_path="/t.jsonl"), run)
    assert _state(run)["activity_path"] == "/t.jsonl"
