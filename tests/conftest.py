"""Real git repositories for the tests that find a run through git.

The run state lives in a worktree's git directory, so a test that writes or
finds one needs a repository with a commit, and sometimes a linked worktree.
Git runs with the operator's global and system configuration switched off, so a
signing key or a hook on the machine cannot change what a test sees.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

_GIT_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=_GIT_ENV, capture_output=True, text=True, check=True
    )
    return result.stdout


@pytest.fixture
def make_repo() -> Callable[[Path], Path]:
    """Build a main worktree with one commit at the given path."""

    def build(path: Path) -> Path:
        path.mkdir(parents=True)
        git(path, "init", "-q", "-b", "main")
        (path / "README.md").write_text("host\n", encoding="utf-8")
        git(path, "add", "README.md")
        git(path, "commit", "-q", "-m", "init")
        return path.resolve()

    return build


@pytest.fixture
def add_worktree() -> Callable[[Path, Path], Path]:
    """Add a linked worktree of ``repo`` at ``path``, on a new branch."""

    def add(repo: Path, path: Path) -> Path:
        git(repo, "worktree", "add", "-q", "-b", path.name, str(path))
        return path.resolve()

    return add
