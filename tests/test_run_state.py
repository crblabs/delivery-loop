"""Starting, reading, resuming a run: the writer the supervisor reads back as ``ok``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import pipeline_state as ps
from core import run_index as ri
from core import run_state as rs
from core import supervisor_decide as sd
from core import supervisor_scan as ss
from core.config import DEFAULTS, StageSpec, load_config


def _state(run: rs.Run) -> dict:
    condition, state = rs.read(run)
    assert condition == "ok", condition
    return state


def test_start_writes_a_state_the_supervisor_reads_as_ok(start_run) -> None:
    # Value: protects=the supervisor keeps working on runs the plugin writes;
    # fails_when=the writer drifts from pipeline_state.valid; why_new=new writer; seam=none
    run, worktree = start_run()
    state = _state(run)
    assert state["status"] == "running"
    assert state["current_stage"] == "autoplan"
    assert state["session_id"] == "s1"
    assert state["repo"] == "local/host"
    assert state["branch"] == "main"
    assert state["driver"] == "plugin"
    assert isinstance(state["guard_baseline"], dict) and state["guard_baseline"]
    records = ss.scan(None, ps.parse_iso(state["updated_at"]), 600, DEFAULTS, per_worktree=True)
    assert [(r["condition"], r["status"], r["index_missing"]) for r in records] == [
        ("ok", "running", False)
    ]
    assert sd.decide(records[0])["action"] == "noop"


def test_start_records_the_index_and_the_config_it_ran_under(start_run) -> None:
    run, worktree = start_run()
    entry = ri.read_entry(ri.index_path(ri.worktree_key(str(worktree))))
    assert entry == {"run_dir": str(run.run_dir), "worktree": str(worktree), "session_id": "s1"}
    assert ri.read_entry(ri.session_path("s1")) == entry
    snapshot = json.loads((run.run_dir / "config.json").read_text(encoding="utf-8"))
    assert snapshot["stages"][0]["name"] == "autoplan"
    assert _state(run)["config_sha256"] == rs.config_hash(run.run_dir)


def test_start_text_is_the_first_stage(make_repo, tmp_path: Path) -> None:
    worktree = make_repo(tmp_path / "host")
    _, text = rs.start(worktree, "CRB-1", DEFAULTS)
    assert text.startswith("Stage 1/5: autoplan. Task: CRB-1.\nRun /autoplan.")
    assert rs.DONE_TOKEN in text and rs.PAUSE_TOKEN in text
    assert "PLAN: the absolute path of the plan file" in text


def test_start_needs_a_task_and_a_worktree(make_repo, tmp_path: Path) -> None:
    worktree = make_repo(tmp_path / "host")
    with pytest.raises(rs.RunError, match="needs a task"):
        rs.start(worktree, "  ", DEFAULTS)
    outside = tmp_path / "plain"
    outside.mkdir()
    with pytest.raises(rs.RunError, match="not in a git worktree"):
        rs.start(outside, "CRB-1", DEFAULTS)


def test_start_refuses_a_second_run_in_one_worktree(start_run) -> None:
    run, worktree = start_run()
    with pytest.raises(rs.RunError, match="already active"):
        rs.start(worktree, "CRB-2", DEFAULTS)


def test_one_session_cannot_start_a_second_run_elsewhere(start_run, make_repo, tmp_path) -> None:
    # Value: protects=every run a session starts is driven by its turn ends; fails_when=a
    # second start rebinds the session and strands the first run; why_new=red-team review;
    # seam=none
    run, worktree = start_run()
    other = make_repo(tmp_path / "other")
    with pytest.raises(rs.RunError, match="already drives the run in"):
        rs.start(other, "CRB-2", DEFAULTS, "s1")
    rs.start(other, "CRB-2", DEFAULTS, "s2")
    ri.abort_run(run.key, run.entry, "person")
    third = make_repo(tmp_path / "third")
    rs.start(third, "CRB-3", DEFAULTS, "s1")


def test_start_after_an_aborted_run_keeps_the_old_state(start_run) -> None:
    run, worktree = start_run()
    old_id = _state(run)["run_id"]
    ri.abort_run(run.key, run.entry, "person")
    new, _ = rs.start(worktree, "CRB-2", DEFAULTS)
    assert (run.run_dir / f"state.{old_id}.json").is_file()
    assert _state(new)["task"] == "CRB-2"


def test_start_cleans_an_index_entry_whose_state_is_gone(start_run) -> None:
    # Value: protects=a recreated worktree can start after its old run was pruned;
    # fails_when=a stale index blocks start forever; why_new=eng E5; seam=none
    run, worktree = start_run()
    run.state_path.unlink()
    new, _ = rs.start(worktree, "CRB-2", DEFAULTS)
    assert _state(new)["task"] == "CRB-2"


def test_start_refuses_the_legacy_install(make_repo, tmp_path: Path) -> None:
    worktree = make_repo(tmp_path / "host")
    (worktree / ".claude/hooks").mkdir(parents=True)
    (worktree / ".claude/hooks/pipeline_stop.py").write_text("#", encoding="utf-8")
    (worktree / ".claude/settings.json").write_text(
        '{"hooks": {"Stop": [{"hooks": [{"command": ".claude/hooks/pipeline_stop.py"}]}]}}',
        encoding="utf-8",
    )
    with pytest.raises(rs.RunError, match="in-repo install.*pipeline_stop.py"):
        rs.start(worktree, "CRB-1", DEFAULTS)


def test_a_leftover_hook_file_not_wired_in_is_not_the_legacy_install(make_repo, tmp_path) -> None:
    worktree = make_repo(tmp_path / "host")
    (worktree / ".claude/hooks").mkdir(parents=True)
    (worktree / ".claude/hooks/pipeline_stop.py").write_text("#", encoding="utf-8")
    rs.start(worktree, "CRB-1", DEFAULTS)


def test_start_refuses_a_stage_with_no_prompt(make_repo, tmp_path: Path) -> None:
    worktree = make_repo(tmp_path / "host")
    config = load_config(None).__class__(stages=(StageSpec(name="build"),))
    with pytest.raises(rs.RunError, match="no prompt file for stage.*build"):
        rs.start(worktree, "CRB-1", config)


def test_a_host_prompt_overrides_the_plugin(make_repo, tmp_path: Path) -> None:
    worktree = make_repo(tmp_path / "host")
    override = worktree / ".claude/skills/pipeline/stages/build.md"
    override.parent.mkdir(parents=True)
    override.write_text("Build it the house way.", encoding="utf-8")
    config = DEFAULTS.__class__(stages=(StageSpec(name="build"), StageSpec(name="ship")))
    _, text = rs.start(worktree, "CRB-1", config)
    assert "Build it the house way." in text


def test_the_repository_comes_from_origin(make_repo, tmp_path: Path, git) -> None:
    worktree = make_repo(tmp_path / "host")
    git(worktree, "remote", "add", "origin", "git@github.com:crblabs/delivery-loop.git")
    assert rs.detect_repo(worktree) == "crblabs/delivery-loop"
    git(worktree, "remote", "set-url", "origin", "https://github.com/a/b")
    assert rs.detect_repo(worktree) == "a/b"


def test_a_fresh_repository_with_no_commit_can_start(tmp_path: Path, git) -> None:
    # Value: protects=the done-when "a new repo can start a run with no files added";
    # fails_when=start needs a remote, a commit or a loop.toml; why_new=plugin; seam=none
    worktree = tmp_path / "fresh"
    worktree.mkdir()
    git(worktree, "init", "-q", "-b", "main")
    run, _ = rs.start(worktree, "CRB-1", DEFAULTS)
    assert _state(run)["repo"] == "local/fresh"


def test_a_run_is_judged_by_the_config_it_started_under(start_run) -> None:
    run, worktree = start_run()
    (worktree / "loop.toml").write_text('[[stages]]\nname = "other"\n', encoding="utf-8")
    reopened = rs.find(str(worktree))
    assert reopened is not None
    assert reopened.config.stage_names == DEFAULTS.stage_names
    assert _state(reopened)["status"] == "running"


def test_a_missing_snapshot_is_reported(start_run) -> None:
    run, _ = start_run()
    (run.run_dir / "config.json").unlink()
    with pytest.raises(rs.RunError, match="no readable config.json"):
        rs.open_run(run.key, run.entry)


def test_update_bumps_the_revision_once_and_refuses_a_corrupt_result(start_run) -> None:
    run, _ = start_run()
    before = _state(run)["revision"]
    rs.update(run, lambda s: s.__setitem__("pr_url", "x"))
    assert _state(run)["revision"] == before + 1
    with pytest.raises(rs.RunError, match="no longer validates"):
        rs.update(run, lambda s: s.__setitem__("status", "bogus"))
    assert _state(run)["revision"] == before + 1


def test_resume_accepts_the_changed_guard_files(start_run) -> None:
    run, _ = start_run()

    def pause(state: dict) -> None:
        state["guard_pending"] = {**state["guard_baseline"], "loop.toml": "new"}
        rs.pause(state, "guard_changed", "Loop guard files changed (loop.toml)")

    rs.update(run, pause)
    assert "Changed guard files: loop.toml" in rs.status_text(run)
    text = rs.resume(run, "person")
    state = _state(run)
    assert state["status"] == "running"
    assert state["guard_baseline"]["loop.toml"] == "new"
    assert state["guard_files_seen"][-1]["loop.toml"] == "new"
    assert state["resumed_by"] == "person"
    assert text.startswith("Stage 1/5: autoplan")


def test_resume_refuses_a_running_run(start_run) -> None:
    run, _ = start_run()
    with pytest.raises(rs.RunError, match="nothing to resume"):
        rs.resume(run, "person")


def test_status_shows_the_last_events(start_run) -> None:
    run, worktree = start_run()
    text = rs.status_text(run)
    assert "Status: running, stage 1/5 autoplan" in text
    assert '"decision": "start"' in text


def test_resume_from_another_session_adopts_the_run(start_run) -> None:
    # Value: protects=a run whose session ended can be picked up from a new one;
    # fails_when=resume keeps the dead session bound; why_new=liveness card says adopt;
    # seam=none
    run, worktree = start_run()
    text = rs.resume(run, "person", "s2")
    assert text.startswith("Stage 1/5")
    state = _state(run)
    assert state["session_id"] == "s2"
    assert state["status"] == "running"
    assert ri.read_entry(ri.session_path("s2"))["worktree"] == str(worktree)
    assert ri.read_entry(ri.session_path("s1")) is None


def test_start_waits_for_another_start_in_the_same_worktree(start_run, make_repo, tmp_path) -> None:
    # Value: protects=two starts in one worktree cannot both pass the check;
    # fails_when=the check and the write are not under one worktree lock; why_new=review
    # adversarial; seam=none
    import fcntl
    import os

    worktree = make_repo(tmp_path / "solo")
    lock_dir = Path(ri.state_home()) / ri.INDEX_DIR / ".locks" / ri.worktree_key(str(worktree))
    lock_dir.mkdir(parents=True)
    fd = os.open(lock_dir / ri.LOCK_FILE, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        import core.run_index as run_index

        original = run_index.PERSON_LOCK_S
        run_index.PERSON_LOCK_S = 0.1
        try:
            with pytest.raises(ri.LockTimeout):
                rs.start(worktree, "CRB-1", DEFAULTS)
        finally:
            run_index.PERSON_LOCK_S = original
    finally:
        os.close(fd)


def test_an_old_state_with_a_forged_run_id_is_kept_in_place(start_run) -> None:
    run, worktree = start_run()
    ri.abort_run(run.key, run.entry, "person")
    state = json.loads(run.state_path.read_text(encoding="utf-8"))
    run.state_path.write_text(json.dumps({**state, "run_id": "../../escape"}), "utf-8")
    rs.start(worktree, "CRB-2", DEFAULTS)
    assert (run.run_dir / "state.old.json").is_file()


# --- Waiting on background tasks ----------------------------------------------


def _waiting(run: rs.Run, tasks: list[str], **more) -> None:
    def apply(state: dict) -> None:
        state["waiting_on"] = tasks
        state["waiting_since"] = ri.now_iso()
        state["wait_turns"][state["current_stage"]] = 3
        state.update(more)

    rs.update(run, apply)


def test_a_resume_from_the_waiting_session_releases_its_tasks(start_run) -> None:
    # Value: protects=a person's way out of a task that will never report, without a
    # pause; fails_when=resume refuses a running run again; why_new=spec review 1; seam=none
    run, _ = start_run()
    _waiting(run, ["a1"])
    text = rs.resume(run, "person", "s1")
    assert text.startswith("Stage 1/5: autoplan")
    state = _state(run)
    assert state["status"] == "running"
    assert state["released_tasks"] == ["a1"] and state["waiting_on"] == []
    assert state["waiting_since"] is None and state["wait_turns"]["autoplan"] == 0
    assert state["resumed_by"] == "person"
    assert state["history"][-1]["reason"] == "waiting"
    assert state["paused_reason"] is None and state["pending_question"] is None


def test_adopting_a_waiting_run_forgets_the_wait_without_releasing(start_run) -> None:
    # Value: protects=a new session's own transcript decides what it waits on, and the
    # supervisor does not escalate the old session's wait; fails_when=adopt keeps
    # waiting_on, or releases tasks; why_new=review red team; seam=none
    run, _ = start_run()
    _waiting(run, ["a1"])
    rs.resume(run, "person", "s2")
    state = _state(run)
    assert state["session_id"] == "s2" and state["status"] == "running"
    assert state["waiting_on"] == [] and state["waiting_since"] is None
    assert state["released_tasks"] == [] and state["wait_turns"]["autoplan"] == 0
    assert state["refused_on"] == []


def test_a_wait_capped_pause_releases_even_from_a_new_session(start_run) -> None:
    # Value: protects=the cap card's promise after /clear; fails_when=adoption keeps the
    # dead tasks; why_new=spec review 2; seam=none
    run, _ = start_run()
    _waiting(run, ["a1", "b2"], wait_capped=True)
    rs.update(run, lambda s: rs.pause(s, "no_message", "card"))
    rs.resume(run, "person", "s2")
    state = _state(run)
    assert state["session_id"] == "s2"
    assert state["released_tasks"] == ["a1", "b2"] and state["wait_capped"] is False


@pytest.mark.parametrize("reason", ["gate", "guard_changed", "needs_human", "no_message"])
def test_any_other_resume_keeps_waiting(start_run, reason) -> None:
    # Value: protects=a reviewer that still runs is still waited for; fails_when=every
    # resume drops live tasks; why_new=spec review 2; seam=none
    run, _ = start_run()
    _waiting(run, ["a1"])
    rs.update(run, lambda s: rs.pause(s, reason, "card"))
    rs.resume(run, "person")
    state = _state(run)
    assert state["waiting_on"] == ["a1"] and state["released_tasks"] == []


def test_released_tasks_are_never_trimmed(start_run) -> None:
    run, _ = start_run()
    for n in range(60):
        _waiting(run, [f"t{n}"])
        rs.resume(run, "person", "s1")
    assert _state(run)["released_tasks"][0] == "t0"


def test_status_shows_what_the_run_waits_on(start_run) -> None:
    run, _ = start_run()
    _waiting(run, ["a1", "b2"])
    text = rs.status_text(run)
    assert "Waiting on a1, b2 since " in text
    assert f"(wait 3 of {rs.MAX_WAITS})" in text


def test_a_state_from_before_the_wait_fields_is_still_valid(start_run) -> None:
    run, _ = start_run()
    state = _state(run)
    keys = ("waiting_on", "waiting_since", "wait_turns", "wait_capped", "released_tasks")
    for key in (*keys, "refused_on", "stuck_on", "activity_path", "waiting_shell_only"):
        state.pop(key)
    state["caps"] = {"attempts": rs.MAX_ATTEMPTS}
    assert ps.valid(state, DEFAULTS)
    assert not ps.valid({**state, "waiting_on": "a1"}, DEFAULTS)


@pytest.mark.parametrize(
    "bad",
    [
        {"waiting_on": [1]},
        {"released_tasks": "a1"},
        {"released_tasks": [None]},
        {"waiting_since": "yesterday"},
        {"waiting_since": 5},
        {"wait_turns": []},
        {"wait_turns": {"autoplan": "1"}},
        {"wait_turns": {"autoplan": -1}},
        {"wait_capped": "yes"},
        {"waiting_shell_only": 1},
        {"activity_path": 5},
        {"refused_on": "a1"},
        {"stuck_on": [3]},
    ],
)
def test_a_malformed_wait_field_makes_the_state_invalid(start_run, bad: dict) -> None:
    # Value: protects=the supervisor and hooks read only well-typed wait fields; fails_when=
    # one of _waits_ok's checks is dropped; why_new=review testing; seam=none
    run, _ = start_run()
    state = _state(run)
    assert ps.valid(state, DEFAULTS)
    assert not ps.valid({**state, **bad}, DEFAULTS)
