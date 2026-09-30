"""The supervisor scanner: discovery through git, conditions, exit codes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from core import pipeline_state as ps
from core import supervisor_scan as ss

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


def test_a_run_leaves_git_status_clean(repo, worktree, git):
    put_state(repo, make_state())
    one = worktree("one", make_state())
    assert git(repo, "status", "--porcelain", "--ignored") == ""
    assert git(one, "status", "--porcelain", "--ignored") == ""


def test_a_removed_worktree_takes_its_state_with_it(repo, worktree, git):
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


# Value: protects=--repo-dir repeats and each repository's runs are all reported once;
#   fails_when=only the last --repo-dir is kept, or one run is listed twice;
#   why_new=every test scans one directory;
#   seam=none
def test_every_repo_dir_is_scanned_and_each_run_reported_once(
    tmp_path, repo, worktree, make_repo, capsys
):
    one = worktree("one", make_state())
    other = make_repo(tmp_path / "other")
    put_state(other, make_state())
    # Two directories of the same repository name the same run twice.
    argv = ["--repo-dir", str(repo), "--repo-dir", str(one), "--repo-dir", str(other)]
    assert ss.main([*argv, "--now", NOW.isoformat()]) == 0
    records = json.loads(capsys.readouterr().out)
    assert sorted(r["worktree"] for r in records) == sorted([str(one), str(other)])


# Value: protects=a bare repository's linked worktrees are found and the bare entry is skipped;
#   fails_when=the bare entry is scanned or its listing breaks the parser;
#   why_new=only non-bare repos are tested;
#   seam=none
def test_a_bare_repository_reports_its_linked_worktrees(tmp_path, repo, add_worktree, git):
    bare = tmp_path / "bare.git"
    git(tmp_path, "clone", "-q", "--bare", str(repo), str(bare))
    linked = add_worktree(bare, tmp_path / "trees" / "task")
    put_state(linked, make_state())
    # A state file where the bare entry's own path would point must not count.
    put_state(bare, make_state())
    records = ss.scan([bare], None, NOW, 600)
    assert [r["worktree"] for r in records] == [str(linked)]


# Value: protects=worktree paths with a newline are parsed intact;
#   fails_when=the -z flag is dropped and entries split on newlines;
#   why_new=every tested path is plain ASCII;
#   seam=none
def test_a_worktree_path_with_a_newline_is_found(tmp_path, repo, add_worktree):
    odd = add_worktree(repo, tmp_path / "odd\ndir" / "task")
    put_state(odd, make_state())
    records = ss.scan([repo], None, NOW, 600)
    assert [r["worktree"] for r in records] == [str(odd)]


# Value: protects=exit 2 (cannot observe) when git is not on PATH;
#   fails_when=a missing git crashes with a traceback and exit 1 (needs attention);
#   why_new=git was never absent in a test;
#   seam=none
def test_a_missing_git_means_cannot_observe(tmp_path, repo):
    empty = tmp_path / "no-git"
    empty.mkdir()
    root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "core.supervisor_scan", "--repo-dir", str(repo)],
        cwd=root,
        env={"PATH": str(empty), "PYTHONPATH": str(root)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "SUPERVISOR_DISCOVERY_FAILED" in result.stderr


# Value: protects=a worktree git marks prunable is not listed;
#   fails_when=the prunable filter is dropped from worktrees();
#   why_new=the deleted-worktree scan test passes through a second skip as well;
#   seam=none
def test_a_prunable_worktree_is_not_listed(repo, worktree):
    gone = worktree("gone", make_state())
    shutil.rmtree(gone)
    listed = [path for path, _ in ss._entries(repo)]
    assert gone not in listed
    assert repo in listed


# Value: protects=a listed worktree whose git dir cannot be read is reported unreadable;
#   fails_when=a rev-parse failure drops the run from the report silently;
#   why_new=every listed worktree in other tests resolves;
#   seam=none
def test_a_worktree_git_cannot_read_is_reported_unreadable(repo, worktree):
    broken = worktree("broken", make_state())
    (broken / ".git").write_text("not a pointer\n", encoding="utf-8")
    records = {r["worktree"]: r for r in ss.scan([repo], None, NOW, 600)}
    assert records[str(broken)] == {"worktree": str(broken), "condition": "unreadable"}


# Value: protects=a worktree whose git dir points into another repository is not trusted;
#   fails_when=the scanner reads a state file from outside the scanned repository;
#   why_new=no test repoints a .git file;
#   seam=none
def test_a_worktree_pointing_at_another_repository_is_reported_unreadable(
    tmp_path, repo, worktree, make_repo, add_worktree
):
    stray = worktree("stray")
    other = make_repo(tmp_path / "other")
    foreign = add_worktree(other, tmp_path / "foreign")
    put_state(foreign, make_state(repo="someone/else"))
    (stray / ".git").write_text(f"gitdir: {ps.git_dir(foreign)}\n", encoding="utf-8")
    records = {r["worktree"]: r for r in ss.scan([repo], None, NOW, 600)}
    assert records[str(stray)]["condition"] == "unreadable"


# Value: protects=an old git that rejects -z is reported with the version loop-scan needs;
#   fails_when=the usage error reads as a generic failure with no upgrade hint;
#   why_new=every test runs a current git;
#   seam=none
def test_a_git_without_z_names_the_version_needed(tmp_path, repo, monkeypatch, capsys):
    shim = tmp_path / "shim"
    shim.mkdir()
    real = shutil.which("git")
    # Stands in for a git older than 2.36: it rejects -z the way git does.
    script = (
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  [ "$a" = -z ] && echo "error: unknown switch" >&2 && exit 129\n'
        "done\n"
        f'exec {real} "$@"\n'
    )
    (shim / "git").write_text(script, encoding="utf-8")
    (shim / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    assert ss.main(["--repo-dir", str(repo)]) == 2
    assert f"needs git {ss.MIN_GIT} or later" in capsys.readouterr().err


# Value: protects=a worktree whose .git points at a sibling's git dir is not given its run;
#   fails_when=the ownership check accepts any directory under <common>/worktrees;
#   why_new=only a pointer into another repository was tested;
#   seam=none
def test_a_worktree_pointing_at_a_sibling_git_dir_is_reported_unreadable(repo, worktree):
    owner = worktree("owner", make_state())
    thief = worktree("thief")
    (thief / ".git").write_text(f"gitdir: {ps.git_dir(owner)}\n", encoding="utf-8")
    records = {r["worktree"]: r for r in ss.scan([repo], None, NOW, 600)}
    assert records[str(thief)] == {"worktree": str(thief), "condition": "unreadable"}
    assert records[str(owner)]["condition"] == "ok"


# Value: protects=a nested worktree with no .git entry is not credited with the main run;
#   fails_when=git's upward search lands on the main git dir and is accepted;
#   why_new=every tested worktree sits outside the main one;
#   seam=none
def test_a_nested_worktree_without_its_git_entry_is_not_given_the_main_run(repo, add_worktree, git):
    put_state(repo, make_state())
    nested = add_worktree(repo, repo / ".worktrees" / "nested")
    git(repo, "worktree", "lock", str(nested))
    (nested / ".git").unlink()
    records = {r["worktree"]: r for r in ss.scan([repo], None, NOW, 600)}
    assert records[str(repo)]["condition"] == "ok"
    assert records[str(nested)] == {"worktree": str(nested), "condition": "unreadable"}


# Value: protects=a locked worktree whose directory is gone raises no alert;
#   fails_when=a missing, locked worktree is reported unreadable on every scan;
#   why_new=only prunable (unlocked) missing worktrees were tested;
#   seam=none
def test_a_locked_worktree_on_a_missing_disk_is_left_out(tmp_path, repo, add_worktree, git, capsys):
    away = add_worktree(repo, tmp_path / "usb" / "task")
    git(repo, "worktree", "lock", "--reason", "usb", str(away))
    shutil.rmtree(tmp_path / "usb")
    assert ss.discover([repo]) == []
    assert ss.main(["--repo-dir", str(repo)]) == 2
    assert "SUPERVISOR_DISCOVERY_EMPTY" in capsys.readouterr().err


# Value: protects=a .git entry that is a pipe cannot hang the scan;
#   fails_when=the pointer read opens anything but a small regular file;
#   why_new=every .git entry in other tests is a file or a directory;
#   seam=none
def test_a_git_entry_that_is_a_pipe_does_not_block_the_scan(repo, worktree):
    odd = worktree("pipe")
    (odd / ".git").unlink()
    os.mkfifo(odd / ".git")
    records = {r["worktree"]: r for r in ss.scan([repo], None, NOW, 600)}
    assert records[str(odd)]["condition"] == "unreadable"


# Value: protects=a binary .git file is reported, not a crash;
#   fails_when=UnicodeDecodeError escapes the pointer read;
#   why_new=the malformed pointer test writes valid text;
#   seam=none
def test_an_undecodable_git_entry_is_reported_unreadable(repo, worktree):
    odd = worktree("binary")
    (odd / ".git").write_bytes(b"\xff\xfe\x00")
    records = {r["worktree"]: r for r in ss.scan([repo], None, NOW, 600)}
    assert records[str(odd)]["condition"] == "unreadable"


# Value: protects=a scan run from inside a git hook lists this repository's runs;
#   fails_when=worktree list or the common dir query inherits GIT_DIR or GIT_WORK_TREE;
#   why_new=the GIT_DIR test covers git_dir only, not the scanner's own git calls;
#   seam=none
def test_a_scan_from_inside_a_git_hook_reports_this_repos_runs(
    tmp_path, repo, worktree, make_repo, monkeypatch
):
    one = worktree("one", make_state())
    other = make_repo(tmp_path / "other")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.worktree")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(other))
    assert [r["worktree"] for r in ss.scan([repo], None, NOW, 600)] == [str(one)]


# Value: protects=an unreadable worktree survives the --repo filter;
#   fails_when=the repo filter drops records that carry no repo field;
#   why_new=every unreadable-worktree test scans with no --repo;
#   seam=none
def test_the_repo_filter_keeps_an_unreadable_worktree(repo, worktree):
    worktree("good", make_state())
    broken = worktree("broken", make_state())
    (broken / ".git").write_text("not a pointer\n", encoding="utf-8")
    records = ss.scan([repo], "crblabs/delivery-loop", NOW, 600)
    assert sorted(r["condition"] for r in records) == ["ok", "unreadable"]


# Value: protects=a loop.toml the loader refuses exits 2 (cannot observe), not a traceback;
#   fails_when=ConfigError escapes main and reads as exit 1 (needs attention);
#   why_new=no scan test passes a bad --config;
#   seam=none
def test_a_refused_config_means_cannot_observe(tmp_path, repo, capsys):
    bad = tmp_path / "loop.toml"
    bad.write_text('[harness]\nworktree_glob = "~/trees/*/*"\n', encoding="utf-8")
    assert ss.main(["--repo-dir", str(repo), "--config", str(bad)]) == 2
    assert "SUPERVISOR_CONFIG_INVALID" in capsys.readouterr().err


# Value: protects=a worktree whose .git is a symlink to another's is not given that run;
#   fails_when=the ownership check resolves the .git entry through the symlink;
#   why_new=the sibling test rewrites the pointer text, not the entry itself;
#   seam=none
def test_a_git_entry_symlinked_to_another_worktree_is_reported_unreadable(repo, worktree):
    owner = worktree("owner", make_state())
    thief = worktree("thief")
    (thief / ".git").unlink()
    (thief / ".git").symlink_to(owner / ".git")
    records = {r["worktree"]: r for r in ss.scan([repo], None, NOW, 600)}
    assert records[str(thief)] == {"worktree": str(thief), "condition": "unreadable"}
    assert records[str(owner)]["condition"] == "ok"


# Value: protects=a worktree git links with relative paths is found with its run;
#   fails_when=the gitdir back-pointer is resolved against the working directory;
#   why_new=every other worktree is added with git's default absolute links;
#   seam=none
def test_a_worktree_with_relative_links_is_found(tmp_path, repo, git):
    relative = ("-c", "worktree.useRelativePaths=true")
    git(repo, *relative, "worktree", "add", "-q", "-b", "rel", "../rel")
    rel = (repo.parent / "rel").resolve()
    if not (repo / ".git" / "worktrees" / "rel" / "gitdir").read_text().startswith(".."):
        pytest.skip("this git writes absolute worktree links only")
    put_state(rel, make_state())
    records = {r["worktree"]: r for r in ss.scan([repo], None, NOW, 600)}
    assert records[str(rel)]["condition"] == "ok"


# Value: protects=config a hook inherits from `git -c` does not reach the scan;
#   fails_when=the GIT_CONFIG_KEY_n and VALUE_n pairs pass through to git;
#   why_new=the hook test also sets GIT_DIR, which alone makes it pass;
#   seam=none
def test_git_c_config_from_a_hook_does_not_reach_the_scan(repo, worktree, monkeypatch):
    one = worktree("one", make_state())
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.bare'='true'")
    assert [r["worktree"] for r in ss.scan([repo], None, NOW, 600)] == [str(one)]
