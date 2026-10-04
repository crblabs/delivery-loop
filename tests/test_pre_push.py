"""The pre-push gate: the Stop hook's rules, run on what a push sends.

Every dash the tests need is built with `chr()`, so this file holds none.
"""

from __future__ import annotations

import io
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from core import pre_push as pp

EM_DASH = chr(0x2014)
PLUGIN = Path(__file__).resolve().parent.parent
ZERO = pp.ZERO


@pytest.fixture
def repo(make_repo: Callable[[Path], Path], tmp_path: Path, monkeypatch) -> Path:
    root = make_repo(tmp_path / "host")
    monkeypatch.chdir(root)
    return root


def _commit(git, root: Path, name: str, text: str, message: str | None = None) -> str:
    (root / name).write_text(text, encoding="utf-8")
    git(root, "add", name)
    git(root, "commit", "-q", "-m", message or f"add {name}")
    return git(root, "rev-parse", "HEAD").strip()


def _push(monkeypatch, local: str, remote: str = ZERO) -> int:
    line = f"refs/heads/main {local} refs/heads/main {remote}\n"
    monkeypatch.setattr("sys.stdin", io.StringIO(line))
    return pp.main(["origin", "url"])


def test_parse_pushes_skips_a_deletion_and_a_malformed_line() -> None:
    text = (
        f"refs/heads/a {'1' * 40} refs/heads/a {ZERO}\n"
        f"refs/heads/b {ZERO} refs/heads/b {'2' * 40}\n"
        "bad\n"
    )
    assert [p.local_ref for p in pp.parse_pushes(text)] == ["refs/heads/a"]


def test_a_clean_push_passes(repo: Path, git, monkeypatch) -> None:
    head = _commit(git, repo, "a.md", "fine\n")
    assert _push(monkeypatch, head) == 0


def test_an_added_dash_refuses_the_push(repo: Path, git, monkeypatch, capsys) -> None:
    head = _commit(git, repo, "a.md", f"x {EM_DASH} y\n")
    assert _push(monkeypatch, head) == 1
    assert "a.md:1:3" in capsys.readouterr().err


def test_a_dash_in_a_pushed_commit_message_refuses_it(repo: Path, git, monkeypatch) -> None:
    head = _commit(git, repo, "a.md", "fine\n", f"add {EM_DASH} a")
    assert _push(monkeypatch, head) == 1


def test_a_message_already_on_the_remote_is_not_refused_again(repo: Path, git, monkeypatch) -> None:
    old = _commit(git, repo, "a.md", "fine\n", f"old {EM_DASH} message")
    head = _commit(git, repo, "b.md", "also fine\n")
    assert _push(monkeypatch, head, remote=old) == 0


def test_the_comment_rule_binds_unless_loop_toml_turns_it_off(repo: Path, git, monkeypatch) -> None:
    long_comment = "# one\n# two\n# three\n# four\nx = 1\n"
    head = _commit(git, repo, "a.py", long_comment)
    assert _push(monkeypatch, head) == 1
    _commit(git, repo, "loop.toml", "[checks]\ncomments = false\n")
    head = _commit(git, repo, "b.py", long_comment)
    assert _push(monkeypatch, head) == 0


def test_a_real_push_runs_the_installed_hook(
    make_repo: Callable[[Path], Path], tmp_path: Path, git
) -> None:
    # Value: protects=a person's own push meets the rules; fails_when=the hook as
    # installed cannot find the gate or ignores its exit code; why_new=port; seam=git
    root = make_repo(tmp_path / "host")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    git(root, "remote", "add", "origin", str(remote))
    hook = root / ".git" / "hooks" / "pre-push"
    hook.write_text(f'#!/bin/sh\nexec "{PLUGIN / "bin" / "loop-pre-push"}" "$@"\n')
    hook.chmod(0o755)
    _commit(git, root, "a.md", f"x {EM_DASH} y\n")
    refused = subprocess.run(
        ["git", "push", "-q", "origin", "main"], cwd=root, capture_output=True, text=True
    )
    assert refused.returncode != 0
    assert "a.md:1:3" in refused.stderr
    # A later commit that removes the dash fixes the branch: the gate reads the
    # branch's diff, not each commit's.
    _commit(git, root, "a.md", "x, y\n", "fix the dash")
    done = subprocess.run(["git", "push", "-q", "origin", "main"], cwd=root, capture_output=True)
    assert done.returncode == 0, done.stderr
