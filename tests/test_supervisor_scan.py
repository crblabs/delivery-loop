"""The supervisor scanner: discovery through git, conditions, exit codes."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import pipeline_state as ps
from core import supervisor_scan as ss
from tests.conftest import git

NOW = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)


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
        "repo": "crblabs/delivery-loop",
        "updated_at": "2026-09-16T09:59:30+00:00",
        "history": [{"event": "started"}],
    }
    state.update(over)
    return state


@pytest.fixture
def repo(tmp_path, make_repo) -> Path:
    return make_repo(tmp_path / "host")


def put_state(wt: Path, state=None, raw=None) -> Path:
    path = ps.state_path(wt)
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    elif state is not None:
        path.write_text(json.dumps(state), encoding="utf-8")
    return wt


@pytest.fixture
def worktree(tmp_path, repo, add_worktree):
    """A linked worktree of the host repository, holding the given state."""

    def make(name: str, state=None, raw=None) -> Path:
        # Anywhere on disk: the scanner assumes no layout.
        wt = add_worktree(repo, tmp_path / "elsewhere" / name / f"task-{name}")
        return put_state(wt, state, raw)

    return make


def test_discovers_the_main_and_every_linked_worktree(repo, worktree):
    put_state(repo, make_state())
    one = worktree("one", make_state())
    two = worktree("two", make_state())
    records = ss.scan([repo], None, NOW, 600)
    assert sorted(r["worktree"] for r in records) == sorted(str(p) for p in (repo, one, two))
    assert {r["condition"] for r in records} == {"ok"}


def test_any_worktree_of_the_repo_finds_every_run(repo, worktree):
    put_state(repo, make_state())
    one = worktree("one", make_state())
    assert len(ss.scan([one], None, NOW, 600)) == 2


def test_a_worktree_with_no_state_is_not_a_run(repo, worktree):
    worktree("idle")
    assert ss.discover([repo]) == []


def test_the_state_lives_in_each_worktree_git_dir(repo, worktree):
    one = worktree("one")
    assert ps.state_path(repo) == repo / ".git" / "delivery-loop" / "state.json"
    assert ps.state_path(one) == repo / ".git" / "worktrees" / "task-one" / "delivery-loop" / (
        "state.json"
    )


def test_a_run_leaves_git_status_clean(repo, worktree):
    put_state(repo, make_state())
    one = worktree("one", make_state())
    assert git(repo, "status", "--porcelain", "--ignored") == ""
    assert git(one, "status", "--porcelain", "--ignored") == ""


def test_a_removed_worktree_takes_its_state_with_it(repo, worktree):
    one = worktree("one", make_state())
    path = ps.state_path(one)
    git(repo, "worktree", "remove", "--force", str(one))
    assert not path.exists()
    assert ss.discover([repo]) == []


def test_a_worktree_deleted_from_disk_is_skipped(repo, worktree):
    put_state(repo, make_state())
    shutil.rmtree(worktree("gone", make_state()))
    assert [r["worktree"] for r in ss.scan([repo], None, NOW, 600)] == [str(repo)]


def test_conditions_reported_not_crashed(worktree, repo):
    worktree("ok", make_state())
    worktree("bad", raw="{broken")
    records = ss.scan([repo], None, NOW, 600)
    conditions = sorted(r["condition"] for r in records)
    assert conditions == ["corrupt", "ok"]


def test_repo_filter_excludes_other_repos(worktree, repo):
    worktree("mine", make_state())
    worktree("other", make_state(repo="someone/else"))
    records = ss.scan([repo], "crblabs/delivery-loop", NOW, 600)
    assert [r["repo"] for r in records] == ["crblabs/delivery-loop"]


def test_age_and_stale(worktree, repo):
    worktree("fresh", make_state(updated_at="2026-09-16T09:59:30+00:00"))
    worktree("old", make_state(updated_at="2026-09-16T09:00:00+00:00"))
    recs = {Path(r["worktree"]).name: r for r in ss.scan([repo], None, NOW, 600)}
    assert recs["task-fresh"]["is_stale"] is False
    assert recs["task-old"]["is_stale"] is True
    assert recs["task-old"]["age_seconds"] == 3600.0


def test_non_utc_updated_at(worktree, repo):
    # 09:00+02:00 is 07:00 UTC; against 10:00 UTC that is 3 hours old.
    worktree("tz", make_state(updated_at="2026-09-16T09:00:00+02:00"))
    rec = ss.scan([repo], None, NOW, 600)[0]
    assert rec["age_seconds"] == 10800.0


def test_exit_codes(tmp_path, worktree, repo, capsys):
    (tmp_path / "plain").mkdir()
    assert ss.main(["--repo-dir", str(tmp_path / "plain")]) == 2
    assert "SUPERVISOR_DISCOVERY_FAILED" in capsys.readouterr().err
    assert ss.main(["--repo-dir", str(repo)]) == 2
    assert "SUPERVISOR_DISCOVERY_EMPTY" in capsys.readouterr().err
    worktree("run", make_state(status="running", updated_at="2026-09-16T09:59:30+00:00"))
    code = ss.main(["--repo-dir", str(repo), "--now", NOW.isoformat()])
    assert code == 0
    worktree("pause", make_state(status="awaiting_human"))
    code = ss.main(["--repo-dir", str(repo), "--now", NOW.isoformat()])
    assert code == 1
    capsys.readouterr()


def test_the_current_directory_is_the_default_repo(worktree, repo, monkeypatch, capsys):
    worktree("run", make_state())
    monkeypatch.chdir(repo)
    assert ss.main(["--now", NOW.isoformat()]) == 0
    capsys.readouterr()
