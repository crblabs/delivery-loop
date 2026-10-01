"""Isolation for every test, and real git repositories for the tests that need one.

Every test gets its own empty state root through ``DELIVERY_LOOP_HOME``, so no
test reads or writes the operator's ``~/.delivery-loop``. Git runs with the
operator's global and system configuration switched off, so a signing key or a
hook on the machine cannot change what a test sees.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from core.pipeline_state import HOME_ENV

_ISOLATED = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


@pytest.fixture(autouse=True)
def home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """This test's own state root, empty at the start."""
    root = tmp_path_factory.mktemp("delivery-loop-home")
    monkeypatch.setenv(HOME_ENV, str(root))
    for name in ("GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in _ISOLATED.items():
        monkeypatch.setenv(name, value)
    return root


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
