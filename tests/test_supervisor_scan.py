"""The supervisor scanner: discovery under the state root, conditions, exit codes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import pipeline_state as ps
from core import supervisor_scan as ss
from core.config import USER_CONFIG_DIR

NOW = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)
REPO = "crblabs/delivery-loop"


def make_state(**over) -> dict:
    state = {
        "version": 1,
        "run_id": "9f1c2e7a-3b4d-4e5f-8a6b-7c8d9e0f1a2b",
        "revision": 3,
        "stages": list(ps.STAGES),
        "current": 1,
        "current_stage": "implement",
        "status": "running",
        "attempts": {s: 0 for s in ps.STAGES},
        "total_attempts": 0,
        "session_id": "abc",
        "repo": REPO,
        "updated_at": "2026-09-16T09:59:30+00:00",
        "history": [{"event": "started"}],
    }
    state.update(over)
    return state


def put_state(worktree: Path, state=None, raw=None, repo: str = REPO) -> Path:
    """Write one run the way the hook does, and return its state file."""
    path = ps.prepare_run_dir(worktree, repo)
    path.write_text(raw if raw is not None else json.dumps(state), encoding="utf-8")
    return path


@pytest.fixture
def worktree(tmp_path):
    """A plain directory standing in for a worktree, holding the given run."""

    def make(name: str, state=None, raw=None, repo: str = REPO) -> Path:
        path = tmp_path / "trees" / name
        path.mkdir(parents=True)
        if state is not None or raw is not None:
            put_state(path, state, raw, repo)
        return path.resolve()

    return make


def records_by_name() -> dict[str, dict]:
    return {Path(r["worktree"]).name: r for r in ss.scan(None, NOW, 600)}


def test_discovers_every_run_of_every_repository(worktree):
    worktree("one", make_state())
    worktree("two", make_state(repo="someone/else"), repo="someone/else")
    records = records_by_name()
    assert set(records) == {"one", "two"}
    assert {r["condition"] for r in records.values()} == {"ok"}


def test_a_worktree_with_no_run_is_not_reported(worktree):
    worktree("idle")
    assert ss.discover() == []


# Value: protects=a run whose worktree was removed is reported as orphaned and needs attention;
#   fails_when=a run outlives its worktree silently;
#   why_new=the state no longer goes away with the worktree;
#   seam=none
def test_a_run_whose_worktree_is_gone_is_orphaned(worktree, capsys):
    worktree("gone", make_state()).rmdir()
    record = records_by_name()["gone"]
    assert record["orphaned"] is True
    assert record["condition"] == "ok"
    assert ss.main(["--now", NOW.isoformat()]) == 1
    # Read under per-run config too, the run stays ok and orphaned, not config_invalid.
    [via_main] = json.loads(capsys.readouterr().out)
    assert (via_main["condition"], via_main["orphaned"]) == ("ok", True)
    assert "error" not in via_main


def test_a_run_that_records_no_worktree_is_unreadable(worktree, home):
    path = put_state(worktree("lost"), make_state())
    (path.parent / ps.WORKTREE_FILE).unlink()
    (record,) = ss.scan(None, NOW, 600)
    assert record == {
        "run_dir": str(path.parent),
        "worktree": None,
        "orphaned": False,
        "condition": "unreadable",
        # The repository the state file names, so --repo can filter the run.
        "repo": REPO,
    }


def test_a_symlinked_run_directory_is_not_read(worktree, home):
    path = put_state(worktree("real"), make_state())
    (home / "runs" / "crblabs-delivery-loop" / "alias").symlink_to(path.parent)
    assert [Path(r["run_dir"]).name for r in ss.scan(None, NOW, 600)] == [path.parent.name]


def test_conditions_reported_not_crashed(worktree):
    worktree("ok", make_state())
    worktree("bad", raw="{broken")
    conditions = sorted(r["condition"] for r in ss.scan(None, NOW, 600))
    assert conditions == ["corrupt", "ok"]


def test_repo_filter_excludes_other_repos(worktree):
    worktree("mine", make_state())
    worktree("other", make_state(repo="someone/else"), repo="someone/else")
    records = ss.scan(REPO, NOW, 600)
    assert [r["repo"] for r in records] == [REPO]


def test_the_repo_filter_keeps_an_unreadable_run(worktree):
    worktree("good", make_state())
    worktree("broken", raw="{broken")
    conditions = sorted(r["condition"] for r in ss.scan(REPO, NOW, 600))
    assert conditions == ["corrupt", "ok"]


def test_age_and_stale(worktree):
    worktree("fresh", make_state(updated_at="2026-09-16T09:59:30+00:00"))
    worktree("old", make_state(updated_at="2026-09-16T09:00:00+00:00"))
    records = records_by_name()
    assert records["fresh"]["is_stale"] is False
    assert records["old"]["is_stale"] is True
    assert records["old"]["age_seconds"] == 3600.0


def test_non_utc_updated_at(worktree):
    # 09:00+02:00 is 07:00 UTC; against 10:00 UTC that is 3 hours old.
    worktree("tz", make_state(updated_at="2026-09-16T09:00:00+02:00"))
    assert records_by_name()["tz"]["age_seconds"] == 10800.0


def test_exit_codes(worktree, capsys):
    assert ss.main([]) == 2
    assert "SUPERVISOR_DISCOVERY_EMPTY" in capsys.readouterr().err
    worktree("run", make_state(status="running", updated_at="2026-09-16T09:59:30+00:00"))
    assert ss.main(["--now", NOW.isoformat()]) == 0
    worktree("pause", make_state(status="awaiting_human"))
    assert ss.main(["--now", NOW.isoformat()]) == 1
    capsys.readouterr()


def test_a_refused_config_means_cannot_observe(tmp_path, capsys):
    bad = tmp_path / "loop.toml"
    bad.write_text('[harness]\nworktree_glob = "~/trees/*/*"\n', encoding="utf-8")
    assert ss.main(["--config", str(bad)]) == 2
    assert "SUPERVISOR_CONFIG_INVALID" in capsys.readouterr().err


# Value: protects=a run leaves git status clean in a main and a linked worktree (CRB-20);
#   fails_when=any run file is written inside the worktree again;
#   why_new=the issue's first done-when item;
#   seam=none
def test_a_run_leaves_git_status_clean(tmp_path, make_repo, add_worktree, git):
    main = make_repo(tmp_path / "host")
    linked = add_worktree(main, tmp_path / "linked")
    put_state(main, make_state())
    put_state(linked, make_state())
    assert git(main, "status", "--porcelain", "--ignored") == ""
    assert git(linked, "status", "--porcelain", "--ignored") == ""
    assert {Path(r["worktree"]) for r in ss.scan(None, NOW, 600)} == {main, linked}


# --- per-run config --------------------------------------------------------------

TWO_STAGES = '[[stages]]\nname = "fix"\n[[stages]]\nname = "ship"\n'


def two_stage_state() -> dict:
    stages = ["fix", "ship"]
    return make_state(
        stages=stages, current=0, current_stage="fix", attempts=dict.fromkeys(stages, 0)
    )


def test_each_run_is_checked_against_its_own_worktree(worktree):
    # Value: protects=a run on a branch with its own [[stages]] reads ok when scanned from
    # another checkout; fails_when=the scan checks every run against one config again;
    # why_new=the other scan tests use one config for all runs; seam=none
    wt = worktree("own", two_stage_state())
    (wt / "loop.toml").write_text(TWO_STAGES, encoding="utf-8")
    [shared] = ss.scan(None, NOW, 600)
    assert shared["condition"] == "corrupt"
    [own] = ss.scan(None, NOW, 600, per_worktree=True)
    assert own["condition"] == "ok"
    assert own["stage_count"] == 2


def test_an_explicit_config_applies_to_every_run(worktree, tmp_path, capsys):
    # Value: protects=with --config the named file judges every run, not each worktree's own;
    # fails_when=main turns per-worktree loading on whatever the flags say;
    # why_new=the per-worktree test calls scan() directly; seam=none
    wt = worktree("own", two_stage_state())
    (wt / "loop.toml").write_text(TWO_STAGES, encoding="utf-8")
    named = tmp_path / "named.toml"
    named.write_text("", encoding="utf-8")
    argv = ["--now", NOW.isoformat()]
    assert ss.main([*argv, "--config", str(named)]) == 1
    assert json.loads(capsys.readouterr().out)[0]["condition"] == "corrupt"
    assert ss.main(argv) == 0
    assert json.loads(capsys.readouterr().out)[0]["condition"] == "ok"


def test_a_bad_worktree_config_is_reported_not_raised(worktree, capsys):
    # Value: protects=one worktree with a broken loop.toml does not stop the scan of the others;
    # fails_when=a ConfigError from one worktree escapes the scan;
    # why_new=the per-worktree lookup is new; seam=none
    worktree("good", make_state())
    bad = worktree("bad", make_state())
    (bad / "loop.toml").write_text("[harness\n", encoding="utf-8")
    assert ss.main(["--now", NOW.isoformat()]) == 1
    records = {Path(r["worktree"]).name: r for r in json.loads(capsys.readouterr().out)}
    assert records["good"]["condition"] == "ok"
    assert records["bad"]["condition"] == "config_invalid"
    assert "not valid TOML" in records["bad"]["error"]


def test_a_broken_run_of_another_repo_is_filtered_out(worktree):
    # Value: protects=--repo drops a config_invalid run that names another repository;
    # fails_when=a config_invalid record loses the repo its state file names;
    # why_new=the repo filter tests cover readable runs only; seam=none
    for name, repo in (("mine", REPO), ("theirs", "someone/else")):
        wt = worktree(name, make_state(repo=repo), repo=repo)
        (wt / "loop.toml").write_text("[harness\n", encoding="utf-8")
    records = ss.scan(REPO, NOW, 600, per_worktree=True)
    assert [(r["condition"], r["repo"]) for r in records] == [("config_invalid", REPO)]


def test_a_worktree_file_cannot_move_the_state(worktree):
    # Value: protects=a branch's loop.toml cannot move the state its hook writes away from
    # where the supervisor reads; fails_when=a worktree file may set state_file again;
    # why_new=the red team reproduced a run vanishing this way; seam=none
    wt = worktree("own", make_state())
    (wt / "loop.toml").write_text('[harness]\nstate_file = "run.json"\n', encoding="utf-8")
    [record] = ss.scan(None, NOW, 600, per_worktree=True)
    assert record["condition"] == "config_invalid"
    assert "state_file is read only from DELIVERY_LOOP_HOME" in record["error"]


def test_a_deep_state_file_does_not_stop_the_scan(worktree):
    # Value: protects=one run with a broken config and a deeply nested state file is reported,
    # not a crash of the whole scan; fails_when=_raw_repo lets RecursionError escape;
    # why_new=two reviewers reproduced the crash; seam=none
    worktree("good", make_state())
    bad = worktree("bad", raw="[" * 100000 + "]" * 100000)
    (bad / "loop.toml").write_text("[harness\n", encoding="utf-8")
    records = ss.scan(None, NOW, 600, per_worktree=True)
    assert sorted(r["condition"] for r in records) == ["config_invalid", "ok"]


@pytest.mark.parametrize("raw", ["[1]", '{"repo": 5}'])
def test_a_broken_run_naming_no_repo_is_kept_under_repo(worktree, raw):
    # Value: protects=a broken run whose state names no repository is still reported under
    # --repo; fails_when=_raw_repo keeps a non-string repo or stops catching a non-object;
    # why_new=only the RecursionError branch of _raw_repo was tested; seam=none
    bad = worktree("bad", raw=raw)
    (bad / "loop.toml").write_text("[harness\n", encoding="utf-8")
    [record] = ss.scan(REPO, NOW, 600, per_worktree=True)
    assert record["condition"] == "config_invalid"
    assert "repo" not in record


def test_a_run_with_no_worktree_record_of_another_repo_is_filtered_out(home):
    # Value: protects=--repo drops another repository's run that records no worktree;
    # fails_when=the no-worktree branch returns before _raw_repo; why_new=the red team
    # reproduced it after the first --repo fix; seam=none
    run = home / ps.RUNS_DIR / "other-repo" / "wt-1"
    run.mkdir(parents=True)
    (run / "state.json").write_text(json.dumps({"repo": "other/repo"}), encoding="utf-8")
    assert ss.scan(REPO, NOW, 600) == []
    [record] = ss.scan("other/repo", NOW, 600)
    assert record["condition"] == "unreadable"


def test_an_orphaned_run_of_another_repo_is_filtered_out(worktree):
    # Value: protects=--repo drops another repository's orphaned run that reads as corrupt;
    # fails_when=an unreadable record loses the repo its state file names; why_new=the red
    # team reproduced cross-repository orphaned alerts; seam=none
    stages = ["fix", "ship"]
    state = make_state(
        repo="someone/else",
        stages=stages,
        current=0,
        current_stage="fix",
        attempts=dict.fromkeys(stages, 0),
    )
    worktree("theirs", state, repo="someone/else").rmdir()
    assert ss.scan(REPO, NOW, 600, per_worktree=True) == []
    [record] = ss.scan("someone/else", NOW, 600, per_worktree=True)
    assert (record["condition"], record["orphaned"]) == ("corrupt", True)


def test_a_run_takes_its_stages_from_the_user_file(worktree, git):
    # Value: protects=a per-worktree run is judged by the stage list in its repository's user
    # file; fails_when=the per-worktree load skips the user file; why_new=the other
    # per-worktree tests use directories that are not git checkouts; seam=none
    wt = worktree("mine", two_stage_state())
    git(wt, "init", "-q")
    user = Path.home() / USER_CONFIG_DIR / "mine.toml"
    user.parent.mkdir(parents=True)
    user.write_text(TWO_STAGES, encoding="utf-8")
    [record] = ss.scan(None, NOW, 600, per_worktree=True)
    assert record["condition"] == "ok"
    assert record["stage_count"] == 2


@pytest.mark.parametrize("key", ["state_root", "state_file"])
def test_a_user_file_cannot_move_the_state(tmp_path, monkeypatch, git, capsys, key):
    # Value: protects=the hook and every command read the run state from one place;
    # fails_when=a user file (read without a flag) sets state_root or state_file;
    # why_new=the red team reproduced runs vanishing when one config moved the state; seam=none
    repo = tmp_path / "mine"
    repo.mkdir()
    git(repo, "init", "-q")
    user = Path.home() / USER_CONFIG_DIR / "mine.toml"
    user.parent.mkdir(parents=True)
    user.write_text(f"[harness]\n{key} = '/elsewhere'\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert ss.main(["--now", NOW.isoformat()]) == 2
    assert f"[harness] {key} is read only from DELIVERY_LOOP_HOME" in capsys.readouterr().err


def test_a_named_config_may_still_move_the_state(worktree, tmp_path, monkeypatch, capsys):
    # Value: protects=an operator can still move the state with --config, as CRB-20 allows;
    # fails_when=the machine-wide keys are refused in a file named with --config;
    # why_new=the refusal above covers implicit files only; seam=none
    monkeypatch.delenv(ps.HOME_ENV)
    moved = tmp_path / "moved-home"
    named = tmp_path / "named.toml"
    named.write_text(f"[harness]\nstate_root = '{moved}'\n", encoding="utf-8")
    monkeypatch.setenv(ps.HOME_ENV, str(moved))
    worktree("far", make_state())
    monkeypatch.delenv(ps.HOME_ENV)
    assert ss.main(["--now", NOW.isoformat(), "--config", str(named)]) == 0
    [record] = json.loads(capsys.readouterr().out)
    assert Path(record["worktree"]).name == "far"


def test_a_long_wait_in_a_quiet_session_is_flagged(worktree, capsys):
    # Value: protects=a stuck background wait reaches a person, and a turn a notification
    # woke is not escalated while it works; fails_when=it is judged by updated_at alone, or
    # by waiting_since alone; why_new=CRB-28 spec review 2, review red team; seam=none
    waiting = {"waiting_on": ["a1"], "waiting_since": "2026-09-16T08:30:00+00:00"}
    worktree("stuck", make_state(updated_at="2026-09-16T08:45:00+00:00", **waiting))
    worktree("woken", make_state(**waiting))
    worktree("recent", make_state(waiting_on=["a1"], waiting_since="2026-09-16T09:30:00+00:00"))
    worktree("unset", make_state(waiting_on=["a1"]))
    worktree("paused", make_state(status="awaiting_human", paused_reason="gate", **waiting))
    records = records_by_name()
    assert records["stuck"]["is_wait_stale"] is True
    assert records["stuck"]["wait_age_s"] == 5400.0
    assert records["stuck"]["waiting_on"] == ["a1"]
    assert records["woken"]["is_wait_stale"] is False
    assert records["recent"]["is_wait_stale"] is False
    assert records["unset"]["is_wait_stale"] is False
    assert records["paused"]["is_wait_stale"] is False
    assert ss._needs_attention(records["stuck"])
    assert not ss._needs_attention(records["recent"])


def test_a_wait_on_shell_commands_alone_is_flagged_sooner(worktree, capsys):
    # Value: protects=review D5: a dev server left running stalls a stage for 15 minutes,
    # not an hour; fails_when=the shell-only flag is ignored; why_new=review; seam=none
    quiet = {"updated_at": "2026-09-16T09:40:00+00:00", "waiting_on": ["b1"]}
    since = {"waiting_since": "2026-09-16T09:40:00+00:00"}
    worktree("shell", make_state(waiting_shell_only=True, **quiet, **since))
    worktree("agent", make_state(**quiet, **since))
    records = records_by_name()
    assert records["shell"]["is_wait_stale"] is True
    assert records["shell"]["waiting_shell_only"] is True
    assert records["agent"]["is_wait_stale"] is False


def test_a_session_that_writes_its_activity_file_is_not_a_stuck_wait(worktree, tmp_path, capsys):
    # Value: protects=review D9: a long turn a notification woke is not escalated while the
    # session works; fails_when=only updated_at decides quiet; why_new=review red team
    import os

    busy, idle = tmp_path / "busy.jsonl", tmp_path / "idle.jsonl"
    for path, at in ((busy, NOW.timestamp() - 60), (idle, NOW.timestamp() - 7200)):
        path.write_text("{}\n", encoding="utf-8")
        os.utime(path, (at, at))
    old = {
        "updated_at": "2026-09-16T08:00:00+00:00",
        "waiting_since": "2026-09-16T08:00:00+00:00",
        "waiting_on": ["a1"],
    }
    worktree("busy", make_state(activity_path=str(busy), **old))
    worktree("idle", make_state(activity_path=str(idle), **old))
    worktree("gone", make_state(activity_path=str(tmp_path / "missing"), **old))
    records = records_by_name()
    assert records["busy"]["is_wait_stale"] is False
    assert records["idle"]["is_wait_stale"] is True
    assert records["gone"]["is_wait_stale"] is True


def test_the_wait_limits_are_read_from_the_command_line(worktree, capsys):
    # Value: protects=the two loop-scan flags reach the rule, at the >= boundary;
    # fails_when=a flag is dropped or the two are swapped; why_new=review testing
    at = "2026-09-16T09:50:00+00:00"
    quiet = {"updated_at": at, "waiting_since": at}
    worktree("agent", make_state(waiting_on=["a1"], **quiet))
    worktree("shell", make_state(waiting_on=["b1"], waiting_shell_only=True, **quiet))
    args = ["--wait-stale-after-seconds", "600", "--shell-wait-stale-after-seconds", "601"]
    assert ss.main(["--now", NOW.isoformat(), *args]) in (0, 1)
    records = {Path(r["worktree"]).name: r for r in json.loads(capsys.readouterr().out)}
    assert records["agent"]["is_wait_stale"] is True
    assert records["shell"]["is_wait_stale"] is False
