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


@pytest.fixture
def repo(tmp_path: Path, make_repo) -> Path:
    return make_repo(tmp_path / "host")


def write(repo: Path, state) -> Path:
    path = ps.state_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def test_ok(repo):
    path = write(repo, make_state())
    condition, state = ps.read_state(path)
    assert condition == "ok"
    assert state["run_id"] == "9f1c2e7a-3b4d-4e5f-8a6b-7c8d9e0f1a2b"


def test_missing(repo):
    assert ps.read_state(ps.state_path(repo)) == ("missing", None)


def test_unreadable_is_a_directory(repo):
    path = ps.state_path(repo)
    path.mkdir(parents=True)
    assert ps.read_state(path)[0] == "unreadable"


def test_corrupt_bad_json(repo):
    path = ps.state_path(repo)
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
    path = ps.state_path(repo)
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


def test_the_main_worktree_keeps_its_state_in_its_git_dir(repo):
    assert ps.state_path(repo) == repo / ".git" / "delivery-loop" / "state.json"


def test_a_linked_worktree_keeps_its_state_in_its_own_git_dir(repo, tmp_path, add_worktree):
    linked = add_worktree(repo, tmp_path / "linked")
    assert ps.git_dir(linked) == repo / ".git" / "worktrees" / "linked"
    assert ps.state_path(linked) == ps.git_dir(linked) / "delivery-loop" / "state.json"
    assert ps.state_path(linked) != ps.state_path(repo)


def test_a_directory_outside_git_has_no_state_path(tmp_path):
    with pytest.raises(ps.NotAWorktree):
        ps.state_path(tmp_path)


# Value: protects=git_dir names the worktree's own git dir even inside a git hook;
#   fails_when=the GIT_* variables leak into the git call again;
#   why_new=no test ran with GIT_DIR exported;
#   seam=none
def test_git_dir_ignores_an_inherited_git_dir(repo, tmp_path, add_worktree, monkeypatch):
    linked = add_worktree(repo, tmp_path / "linked")
    (linked / "sub").mkdir()
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    # A subdirectory has no .git entry of its own, so this path asks git.
    assert ps.git_dir(linked / "sub") == repo / ".git" / "worktrees" / "linked"
    assert ps.git_dir(linked) == repo / ".git" / "worktrees" / "linked"


# Value: protects=a linked worktree's git dir is read from its .git pointer file;
#   fails_when=the pointer is misread, or a relative gitdir is not joined to the worktree;
#   why_new=no test writes a relative gitdir pointer;
#   seam=none
def test_a_relative_gitdir_pointer_is_resolved_from_the_worktree(repo, tmp_path, add_worktree):
    linked = add_worktree(repo, tmp_path / "linked")
    target = repo / ".git" / "worktrees" / "linked"
    (linked / ".git").write_text(f"gitdir: {os.path.relpath(target, linked)}\n", encoding="utf-8")
    assert ps.git_dir(linked) == target


# Value: protects=git_dir reports a missing git as NotAWorktree, which adapters handle;
#   fails_when=OSError escapes git_dir when git cannot be started;
#   why_new=the missing-git scan test fails earlier, in the scanner;
#   seam=none
def test_git_dir_without_git_raises_not_a_worktree(repo, tmp_path, monkeypatch):
    (repo / "sub").mkdir()
    empty = tmp_path / "no-git"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    with pytest.raises(ps.NotAWorktree):
        ps.git_dir(repo / "sub")
