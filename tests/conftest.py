"""Real git repositories for the tests that find a run through git.

The run state lives in a worktree's git directory, so a test that writes or
finds one needs a repository with a commit, and sometimes a linked worktree.
Every git call, the fixtures' own and the code's under test, runs with the
operator's global and system configuration switched off and with no variable
that points git at another repository. A signing key, a hook on the machine, or
a test run from inside a git hook cannot change what a test sees.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from core.pipeline_state import git_env

_ISOLATED = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


@pytest.fixture(autouse=True)
def _isolated_git(monkeypatch: pytest.MonkeyPatch) -> None:
    # Scrub exactly what the code under test scrubs.
    for name in set(os.environ) - set(git_env()):
        monkeypatch.delenv(name, raising=False)
    for name, value in _ISOLATED.items():
        monkeypatch.setenv(name, value)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return result.stdout


@pytest.fixture
def git() -> Callable[..., str]:
    """Run one git command in a directory and return its output."""
    return _git


@pytest.fixture
def make_repo() -> Callable[[Path], Path]:
    """Build a main worktree with one commit at the given path."""

    def build(path: Path) -> Path:
        path.mkdir(parents=True)
        _git(path, "init", "-q", "-b", "main")
        (path / "README.md").write_text("host\n", encoding="utf-8")
        _git(path, "add", "README.md")
        _git(path, "commit", "-q", "-m", "init")
        return path.resolve()

    return build


@pytest.fixture
def add_worktree() -> Callable[[Path, Path], Path]:
    """Add a linked worktree of ``repo`` at ``path``, on a new branch."""

    def add(repo: Path, path: Path) -> Path:
        _git(repo, "worktree", "add", "-q", "-b", path.name, str(path))
        return path.resolve()

    return add
