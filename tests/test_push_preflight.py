"""``core/push_preflight.py``: origin rerouted to HTTPS, narrowly and once.

Most branches run against ``FakeGit``, which holds the config keys the helper
touches and scripted outcomes, so a lock race or a network timeout runs
offline and the same every time. The real-git tests at the end check the same
rewrite against an actual repository, reading "HTTPS" from a bare repository on
disk through ``url.insteadOf``.

Each test names the failure it prevents: pushing over a dead SSH transport, a
stray pushurl silently keeping SSH, several pushurls surviving a "successful"
repair, a remote that is not GitHub being clobbered, and a config-lock race
between two worktrees starting at once.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core import push_preflight as pf

SSH = "git@github.com:owner/repo.git"
CANON = "https://github.com/owner/repo.git"


class FakeGit:
    """Stands in for ``push_preflight._git``. ``remote.origin.pushurl`` is a list,
    because git allows several values. ``lock_fails`` makes the first N config
    writes fail with a config.lock error, to exercise the retry."""

    def __init__(self, *, url, pushurls=None, ls_ok=True, ls_timeout=False, lock_fails=0):
        self.url = url
        self.pushurls: list[str] = list(pushurls or [])
        self.ls_ok = ls_ok
        self.ls_timeout = ls_timeout
        self.lock_fails = lock_fails
        self.calls: list[list[str]] = []

    def __call__(self, args, cwd, timeout=10):
        self.calls.append(args)
        if args[:1] == ["ls-remote"]:
            return self._ls_remote()
        if args[:3] == ["remote", "set-url", "origin"]:
            return self._set_url(args[3])
        return self._config(args)

    def _config(self, args):
        if args[:3] == ["config", "--get", "remote.origin.url"]:
            return _cp(0 if self.url else 1, self.url or "")
        if args[:3] == ["config", "--get-all", "remote.origin.pushurl"]:
            return _cp(0 if self.pushurls else 1, "\n".join(self.pushurls))
        if args[:3] == ["config", "--unset-all", "remote.origin.pushurl"]:
            return self._unset_pushurls()
        raise AssertionError(f"unexpected git call: {args}")

    def _set_url(self, url):
        if self.lock_fails > 0:
            self.lock_fails -= 1
            return _cp(1, "error: could not lock config file .git/config: File exists")
        self.url = url
        return _cp(0, "")

    def _unset_pushurls(self):
        if not self.pushurls:
            return _cp(5, "")
        self.pushurls = []
        return _cp(0, "")

    def _ls_remote(self):
        if self.ls_timeout:
            return _cp(pf._TIMEOUT_RC, "TIMEOUT")
        return _cp(0, "abc\trefs/heads/main") if self.ls_ok else _cp(128, "not found")


def _cp(code, out):
    return subprocess.CompletedProcess(args=["git"], returncode=code, stdout=out, stderr=out)


def _install(monkeypatch, fake):
    monkeypatch.setattr(pf, "_git", fake)
    monkeypatch.setattr(pf, "_CONFIG_RETRY_S", 0.5)
    monkeypatch.setattr(pf, "_CONFIG_POLL_S", 0.01)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (SSH, CANON),
        ("git@github.com:owner/repo", CANON),
        (CANON, CANON),
        ("https://github.com/owner/repo", CANON),
        ("git@github.com:other/thing.git", "https://github.com/other/thing.git"),
        ("git@gitlab.com:owner/repo.git", None),
        ("ssh://git@example.org/owner/repo.git", None),
        ("/srv/repos/repo.git", None),
        ("git@github.com:no-slash", None),
    ],
)
def test_the_https_url_is_derived_from_origin_never_hardcoded(url, expected):
    assert pf.https_url(url) == expected


def test_ssh_origin_is_rewritten_to_https(monkeypatch):
    fake = FakeGit(url=SSH)
    _install(monkeypatch, fake)
    result = pf.ensure_https_origin(Path("/wt"))
    assert result.status == "fixed"
    assert result.passed
    assert fake.url == CANON


def test_the_rewrite_keeps_the_owner_and_name_origin_names(monkeypatch):
    fake = FakeGit(url="git@github.com:someone/elsewhere")
    _install(monkeypatch, fake)
    assert pf.ensure_https_origin(Path("/wt")).status == "fixed"
    assert fake.url == "https://github.com/someone/elsewhere.git"


@pytest.mark.parametrize("url", ["git@gitlab.com:owner/repo.git", "/srv/repo.git", ""])
def test_a_remote_that_is_not_github_is_refused_not_clobbered(monkeypatch, url):
    fake = FakeGit(url=url)
    _install(monkeypatch, fake)
    result = pf.ensure_https_origin(Path("/wt"))
    assert result.status == "unexpected_remote"
    assert not result.passed
    assert "PREFLIGHT_PUSH_ORIGIN" in result.message
    assert fake.url == url
    assert not any(c[:1] == ["remote"] or c[:1] == ["ls-remote"] for c in fake.calls)


@pytest.mark.parametrize("url", [CANON, CANON.removesuffix(".git")])
def test_already_https_is_an_idempotent_noop(monkeypatch, url):
    fake = FakeGit(url=url)
    _install(monkeypatch, fake)
    result = pf.ensure_https_origin(Path("/wt"))
    assert result.status == "ok"
    assert not any(c[:3] == ["remote", "set-url", "origin"] for c in fake.calls)


def test_an_https_pushurl_over_an_ssh_fetch_still_fixes_the_fetch(monkeypatch):
    # Gating on the push URL alone would skip the fetch fix, and then removing
    # the pushurl would drop push back to the SSH fetch URL.
    fake = FakeGit(url=SSH, pushurls=[CANON])
    _install(monkeypatch, fake)
    assert pf.ensure_https_origin(Path("/wt")).status == "fixed"
    assert fake.url == CANON
    assert fake.pushurls == []


def test_every_pushurl_is_removed(monkeypatch):
    # --unset, not --unset-all, would refuse a key with several values.
    fake = FakeGit(url=CANON, pushurls=[SSH, "git@github.com:someone/fork.git"])
    _install(monkeypatch, fake)
    assert pf.ensure_https_origin(Path("/wt")).status == "fixed"
    assert fake.pushurls == []


def test_an_unreachable_remote_is_named_not_silent(monkeypatch):
    _install(monkeypatch, FakeGit(url=CANON, ls_ok=False))
    result = pf.ensure_https_origin(Path("/wt"))
    assert result.status == "unreachable"
    assert "PREFLIGHT_PUSH_UNREACHABLE" in result.message


def test_an_ls_remote_timeout_is_unreachable(monkeypatch):
    _install(monkeypatch, FakeGit(url=CANON, ls_timeout=True))
    result = pf.ensure_https_origin(Path("/wt"))
    assert result.status == "unreachable"
    assert "did not answer in time" in result.message


def test_config_lock_contention_retries_then_succeeds(monkeypatch):
    # git's config.lock is the mutex; a couple of contended attempts then win.
    fake = FakeGit(url=SSH, lock_fails=2)
    _install(monkeypatch, fake)
    assert pf.ensure_https_origin(Path("/wt")).status == "fixed"
    assert fake.url == CANON


def test_a_persistent_config_lock_reports_lock_contended(monkeypatch):
    fake = FakeGit(url=SSH, lock_fails=9999)
    _install(monkeypatch, fake)
    result = pf.ensure_https_origin(Path("/wt"))
    assert result.status == "lock_contended"
    assert "PREFLIGHT_PUSH_LOCK" in result.message
    assert fake.url == SSH


def test_the_git_wrapper_turns_a_timeout_into_a_returncode(monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(pf.subprocess, "run", boom)
    assert pf._git(["config", "--get", "x"], Path("/wt")).returncode == pf._TIMEOUT_RC


# --- against a real repository -------------------------------------------------


def _config(git, repo: Path, key: str) -> list[str]:
    try:
        return git(repo, "config", "--get-all", key).split()
    except subprocess.CalledProcessError:
        return []


def test_a_real_ssh_origin_is_rewritten_once(make_repo, git, github_origin, tmp_path):
    repo = make_repo(tmp_path / "host")
    github_origin(repo, SSH)
    git(repo, "config", "--add", "remote.origin.pushurl", SSH)
    git(repo, "config", "--add", "remote.origin.pushurl", "git@github.com:owner/fork.git")
    first = pf.ensure_https_origin(repo)
    assert first.status == "fixed", first.message
    assert _config(git, repo, "remote.origin.url") == [CANON]
    assert _config(git, repo, "remote.origin.pushurl") == []
    second = pf.ensure_https_origin(repo)
    assert second.status == "ok", second.message


def test_a_linked_worktree_rewrites_the_shared_origin(
    make_repo, add_worktree, git, github_origin, tmp_path
):
    # The remote lives in the shared config, so one start fixes every worktree.
    repo = make_repo(tmp_path / "host")
    github_origin(repo, SSH)
    linked = add_worktree(repo, tmp_path / "linked")
    assert pf.ensure_https_origin(linked).status == "fixed"
    assert _config(git, repo, "remote.origin.url") == [CANON]


def test_a_real_unreachable_origin_is_reported(make_repo, git, tmp_path):
    repo = make_repo(tmp_path / "host")
    git(repo, "remote", "add", "origin", SSH)
    # No mirror: github.com is sent to a directory that holds no repository.
    git(repo, "config", f"url.{(tmp_path / 'nothing').as_uri()}/.insteadOf", "https://github.com/")
    result = pf.ensure_https_origin(repo)
    assert result.status == "unreachable"
    # The rewrite is kept: it is right whatever the network says, and idempotent.
    assert _config(git, repo, "remote.origin.url") == [CANON]


@pytest.mark.parametrize(
    ("url", "pushurl", "level"),
    [
        (CANON, None, "ok"),
        (SSH, None, "warn"),
        (CANON, SSH, "warn"),
        ("git@gitlab.com:owner/repo.git", None, "fail"),
        (None, None, "fail"),
    ],
)
def test_describe_origin_reads_and_changes_nothing(make_repo, git, tmp_path, url, pushurl, level):
    repo = make_repo(tmp_path / "host")
    if url:
        git(repo, "remote", "add", "origin", url)
    if pushurl:
        git(repo, "config", "remote.origin.pushurl", pushurl)
    assert pf.describe_origin(repo)[0] == level
    assert _config(git, repo, "remote.origin.url") == ([url] if url else [])
