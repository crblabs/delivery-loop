"""The supervisor's state reader validates every field it exposes."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from core import pipeline_state as ps


def make_state(**over) -> dict:
    state = {
        "version": 1,
        "run_id": "9f1c2e7a-3b4d-4e5f-8a6b-7c8d9e0f1a2b",
        "revision": 3,
        "stages": list(ps.STAGES),
        "current": 0,
        "current_stage": "autoplan",
        "status": "running",
        "attempts": {s: 0 for s in ps.STAGES},
        "total_attempts": 0,
        "session_id": None,
        "updated_at": "2026-09-16T09:42:11.000000+00:00",
        "history": [],
    }
    state.update(over)
    return state


REPO = "crblabs/host"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A worktree. The state reader needs no git, only a path."""
    path = tmp_path / "host"
    path.mkdir()
    return path


def write(repo: Path, state) -> Path:
    path = ps.state_path(repo, REPO)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def test_ok(repo):
    path = write(repo, make_state())
    condition, state = ps.read_state(path)
    assert condition == "ok"
    assert state["run_id"] == "9f1c2e7a-3b4d-4e5f-8a6b-7c8d9e0f1a2b"


def test_missing(repo):
    assert ps.read_state(ps.state_path(repo, REPO)) == ("missing", None)


def test_unreadable_is_a_directory(repo):
    path = ps.state_path(repo, REPO)
    path.mkdir(parents=True)
    assert ps.read_state(path)[0] == "unreadable"


def test_corrupt_bad_json(repo):
    path = ps.state_path(repo, REPO)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert ps.read_state(path)[0] == "corrupt"


def test_corrupt_bad_schema(repo):
    path = write(repo, make_state(status="banana"))
    assert ps.read_state(path)[0] == "corrupt"


def test_unsupported_version(repo):
    path = write(repo, make_state(version=2))
    assert ps.read_state(path)[0] == "unsupported"


def test_huge_int_literal_is_corrupt_not_crash(repo):
    # A 5000-digit int raises ValueError inside json, not JSONDecodeError.
    path = ps.state_path(repo, REPO)
    path.parent.mkdir(parents=True)
    path.write_text('{"version":' + "9" * 5000 + "}", encoding="utf-8")
    assert ps.read_state(path)[0] == "corrupt"


@pytest.mark.parametrize(
    "value,ok",
    [
        ("2026-09-16T09:42:11+00:00", True),
        ("2026-09-16T09:42:11Z", True),
        ("2026-09-16T09:42:11", False),
        ("not-a-date", False),
        (None, False),
    ],
)
def test_parse_iso(value, ok):
    assert (ps.parse_iso(value) is not None) is ok


def test_the_state_lives_under_the_home_not_the_worktree(repo, home):
    path = ps.state_path(repo, REPO)
    assert path.parent.parent == home / "runs" / "crblabs-host"
    assert path.parent.name.startswith("host-")
    assert path.name == "state.json"
    assert repo not in path.parents


# Value: protects=two worktrees with the same folder name never share a run directory;
#   fails_when=the run directory is named after the folder alone;
#   why_new=every other test uses one folder name per repository;
#   seam=none
def test_two_worktrees_with_one_folder_name_get_two_run_directories(tmp_path):
    one = tmp_path / "a" / "task"
    two = tmp_path / "b" / "task"
    one.mkdir(parents=True)
    two.mkdir(parents=True)
    assert ps.run_dir(one, REPO) != ps.run_dir(two, REPO)
    assert ps.run_dir(one, REPO) == ps.run_dir(one, REPO)


def test_the_home_moves_with_the_environment_and_the_config(repo, tmp_path, monkeypatch):
    from core import config as cfg

    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv(ps.HOME_ENV, str(elsewhere))
    assert ps.state_home() == elsewhere
    monkeypatch.delenv(ps.HOME_ENV)
    assert ps.state_home() == Path("~/.delivery-loop").expanduser()
    assert ps.state_home(cfg.LoopConfig(state_root=str(tmp_path / "cfg"))) == tmp_path / "cfg"


# Value: protects=a repository name never escapes or splits the runs directory;
#   fails_when=a slash or dot-dot in the repository name reaches the path unchanged;
#   why_new=every other test uses a plain owner/name;
#   seam=none
@pytest.mark.parametrize("repo_name", ["../../etc", "owner/../../x", "a b/c:d"])
def test_a_hostile_repository_name_stays_one_directory(repo, home, repo_name):
    directory = ps.run_dir(repo, repo_name)
    assert directory.parent.parent == home / "runs"
    assert directory.parent.name not in (".", "..")


def test_a_run_needs_its_repository(repo):
    with pytest.raises(ValueError):
        ps.run_dir(repo, "  ")


def test_prepare_records_the_worktree_and_returns_the_state_path(repo):
    path = ps.prepare_run_dir(repo, REPO)
    assert path == ps.state_path(repo, REPO)
    assert ps.read_worktree(path.parent) == repo.resolve()


@pytest.mark.parametrize("content", ["", "relative/path\n", "/a\n/b\n"])
def test_an_unusable_worktree_record_reads_as_none(repo, content):
    directory = ps.prepare_run_dir(repo, REPO).parent
    (directory / ps.WORKTREE_FILE).write_text(content, encoding="utf-8")
    assert ps.read_worktree(directory) is None


# Value: protects=a worktree record that is a pipe cannot hang the reader;
#   fails_when=the record is read without the regular-file check;
#   why_new=every other record is a regular file;
#   seam=none
def test_a_worktree_record_that_is_a_pipe_reads_as_none(repo):
    directory = ps.prepare_run_dir(repo, REPO).parent
    (directory / ps.WORKTREE_FILE).unlink()
    os.mkfifo(directory / ps.WORKTREE_FILE)
    assert ps.read_worktree(directory) is None
