"""The supervisor scanner: discovery under the state root, conditions, exit codes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import pipeline_state as ps
from core import supervisor_scan as ss

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
    capsys.readouterr()


def test_a_run_that_records_no_worktree_is_unreadable(worktree, home):
    path = put_state(worktree("lost"), make_state())
    (path.parent / ps.WORKTREE_FILE).unlink()
    (record,) = ss.scan(None, NOW, 600)
    assert record == {
        "run_dir": str(path.parent),
        "worktree": None,
        "orphaned": False,
        "condition": "unreadable",
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
