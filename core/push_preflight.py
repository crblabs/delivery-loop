#!/usr/bin/env python3
"""Route ``origin`` to HTTPS before a run, when the host asks for it.

Some worktree managers leave ``origin`` as a GitHub SSH URL whose key has no
push access, while HTTPS to the same repository works through a credential
helper they inject. The last stage of a run pushes, so with that SSH origin it
fails at the very end. With ``[repo] push_transport = "https"`` in loop.toml,
``start`` calls ``ensure_https_origin`` first and refuses to start when it
fails. With the default, empty, nothing here runs and origin is left alone.

Three facts make this subtle:

  * ``remote.origin.url`` lives in the repository's shared config, so the
    change reaches every worktree. That is intended: the SSH key is the same in
    all of them. A machine-wide ``url.insteadOf`` is not used, because it would
    rewrite other repositories too.
  * ``git remote set-url`` sets the fetch URL only. A stray
    ``remote.origin.pushurl`` would still send pushes over SSH, so every
    pushurl value is removed as well.
  * Two worktrees starting at once race on git's own ``config.lock``. That lock
    is the right mutex for config writes, so a write is retried for a few
    seconds while it is held, and the lock file is never deleted to recover.

The HTTPS URL is derived from the current origin, ``git@github.com:owner/repo``
becoming ``https://github.com/owner/repo.git``, so no repository is named here.
An origin that is not on github.com, or that does not parse, is reported and
never rewritten. The closing ``git ls-remote`` proves the remote can be read;
only the run's own push proves it can be written.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from core.config import git_env

_GIT_TIMEOUT_S = 10
_NET_TIMEOUT_S = 20
# The returncode a call gets when it ran past its deadline, as timeout(1) uses.
_TIMEOUT_RC = 124
# What git prints when another process holds its config.lock.
_LOCK_HINTS = ("config.lock", "could not lock", "unable to lock", "File exists")
_CONFIG_RETRY_S = 5.0
_CONFIG_POLL_S = 0.15
# git exits 5 from --unset-all when the key is already gone, as after a
# concurrent start removed it first.
_UNSET_ABSENT_RC = 5

_GH_SSH_RE = re.compile(r"^git@github\.com:(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$")
_GH_HTTPS_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+?)(?:\.git)?/?$"
)


@dataclass(frozen=True)
class PreflightResult:
    """``status`` is ok, fixed, unexpected_remote, lock_contended, unreachable or error."""

    status: str
    message: str

    @property
    def passed(self) -> bool:
        return self.status in ("ok", "fixed")


def _git(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run git and never raise: a timeout or a spawn failure becomes a returncode,
    so every caller classifies a failure the same way."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=git_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, _TIMEOUT_RC, "", "TIMEOUT")
    except OSError as exc:
        return subprocess.CompletedProcess(args, 127, "", str(exc))


def _git_config_write(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """A config-changing git call, retried while git's config.lock is held."""
    deadline = time.monotonic() + _CONFIG_RETRY_S
    while True:
        done = _git(args, cwd)
        if done.returncode == 0:
            return done
        if any(h in done.stderr for h in _LOCK_HINTS) and time.monotonic() < deadline:
            time.sleep(_CONFIG_POLL_S)
            continue
        return done


def https_url(url: str) -> str | None:
    """The HTTPS URL of the GitHub repository ``url`` names, SSH or HTTPS; None
    for any other host or a URL that does not parse."""
    match = _GH_SSH_RE.match(url) or _GH_HTTPS_RE.match(url)
    if match is None:
        return None
    return f"https://github.com/{match.group('owner')}/{match.group('repo')}.git"


def _is_https(url: str, canonical: str) -> bool:
    base = canonical.removesuffix(".git")
    return url in (base, canonical, base + "/")


def origin_url(worktree: Path) -> str | None:
    """The configured fetch URL of origin, as written: ``remote get-url`` would
    apply ``insteadOf`` and show a URL that is not the one to repair."""
    done = _git(["config", "--get", "remote.origin.url"], worktree)
    url = done.stdout.strip()
    return url if done.returncode == 0 and url else None


def _write_failure(done: subprocess.CompletedProcess) -> PreflightResult:
    if done.returncode == _TIMEOUT_RC or any(h in done.stderr for h in _LOCK_HINTS):
        return PreflightResult(
            "lock_contended",
            "PREFLIGHT_PUSH_LOCK: the shared git config is busy (another start is "
            "configuring the remote). Fix: start again in a moment.",
        )
    return PreflightResult(
        "error",
        f"PREFLIGHT_PUSH_CONFIG: git config failed: {done.stderr.strip() or 'unknown error'}",
    )


def ensure_https_origin(worktree: Path) -> PreflightResult:
    """Point origin, fetch and push, at the HTTPS URL of the repository it names.

    It rewrites the shared ``remote.origin.url`` only when origin is a GitHub
    repository over SSH, and keeps the same owner and name. It removes every
    ``remote.origin.pushurl`` value, so push follows the fetch URL. Running it
    again changes nothing. It never raises: every failure is a status with a
    message that says what to do.
    """
    fetch = _git(["config", "--get", "remote.origin.url"], worktree)
    if fetch.returncode == _TIMEOUT_RC:
        return _write_failure(fetch)
    url = fetch.stdout.strip()
    canonical = https_url(url) if url else None
    if canonical is None:
        return _unexpected_remote(url)
    changed, failure = _reroute(worktree, url, canonical)
    if failure is not None:
        return failure
    failure = _unreachable(worktree)
    if failure is not None:
        return failure
    how = "set by this start" if changed else "already set"
    return PreflightResult(
        "fixed" if changed else "ok", f"origin publishes over HTTPS ({canonical}; {how})."
    )


def _unexpected_remote(url: str) -> PreflightResult:
    shown = repr(url) if url else "not set"
    return PreflightResult(
        "unexpected_remote",
        f"PREFLIGHT_PUSH_ORIGIN: origin is {shown}, not a GitHub repository over SSH "
        'or HTTPS, so push_transport = "https" cannot reroute it. Fix: set origin by '
        "hand (git remote set-url origin https://github.com/owner/repo.git), or remove "
        "push_transport from loop.toml.",
    )


def _reroute(worktree: Path, url: str, canonical: str) -> tuple[bool, PreflightResult | None]:
    """Whether origin's URLs changed, and the failure that stopped the change."""
    changed = False
    if not _is_https(url, canonical):
        done = _git_config_write(["remote", "set-url", "origin", canonical], worktree)
        if done.returncode != 0:
            return changed, _write_failure(done)
        changed = True
    # --unset-all, not --unset: --unset refuses a key with several values and
    # would leave an extra push destination behind.
    pushurls = _git(["config", "--get-all", "remote.origin.pushurl"], worktree)
    if pushurls.stdout.strip():
        done = _git_config_write(["config", "--unset-all", "remote.origin.pushurl"], worktree)
        if done.returncode not in (0, _UNSET_ABSENT_RC):
            return changed, _write_failure(done)
        changed = True
    return changed, None


def _unreachable(worktree: Path) -> PreflightResult | None:
    listed = _git(["ls-remote", "--heads", "origin"], worktree, timeout=_NET_TIMEOUT_S)
    if listed.returncode == 0:
        return None
    detail = (
        "it did not answer in time"
        if listed.returncode == _TIMEOUT_RC
        else listed.stderr.strip() or f"git exited {listed.returncode}"
    )
    return PreflightResult(
        "unreachable",
        f"PREFLIGHT_PUSH_UNREACHABLE: cannot read origin over HTTPS ({detail}). "
        "Fix: check the network and the HTTPS credential helper, then start again.",
    )


def describe_origin(worktree: Path) -> tuple[str, str]:
    """``(level, detail)`` for a check that only reads: ok when origin pushes over
    HTTPS already, warn when start will reroute it, fail when it cannot."""
    url = origin_url(worktree)
    canonical = https_url(url) if url else None
    if canonical is None:
        shown = url or "not set"
        return "fail", f"origin is {shown}, not a GitHub repository; start will refuse"
    pushurls = _git(["config", "--get-all", "remote.origin.pushurl"], worktree).stdout.split()
    if _is_https(url, canonical) and not pushurls:
        return "ok", f"origin pushes over HTTPS ({url})"
    return "warn", f"origin is {url}; start will reroute it to {canonical}"
