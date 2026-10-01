"""The guard map: bounded work, and every file that can change the run's rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from core import guard_evidence as ge
from core import run_index as ri
from core.config import DEFAULTS, LoopConfig


def test_a_loop_prefix_stops_at_the_file_cap(tmp_path: Path, monkeypatch) -> None:
    # Value: protects=a Stop hook stays inside its timeout however many files the agent
    # adds; fails_when=the cap only breaks the inner loop; why_new=review; seam=none
    monkeypatch.setattr(ge, "MAX_PREFIX_FILES", 5)
    base = tmp_path / ".claude/skills/pipeline"
    for d in range(4):
        (base / f"d{d}").mkdir(parents=True)
        for f in range(4):
            (base / f"d{d}" / f"{f}.md").write_text("x", encoding="utf-8")
    paths = ge.guard_paths(tmp_path, DEFAULTS)
    in_prefix = [p for p in paths if p.startswith(".claude/skills/pipeline/")]
    assert len(in_prefix) == 6
    assert ".claude/skills/pipeline/*" in in_prefix


def test_files_past_the_byte_budget_are_recorded_by_size_and_time(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(ge, "MAX_FILE_BYTES", 10)
    (tmp_path / "loop.toml").write_text("x" * 50, encoding="utf-8")
    values = ge.guard_map(tmp_path, DEFAULTS, tmp_path)
    assert values["loop.toml"].startswith("stat:50:")


def test_the_git_pointer_and_config_of_a_linked_worktree_are_in_the_map(
    make_repo, add_worktree, tmp_path: Path
) -> None:
    repo = make_repo(tmp_path / "main")
    linked = add_worktree(repo, tmp_path / "linked")
    values = ge.guard_map(linked, DEFAULTS, tmp_path)
    assert values[ge.GIT_POINTER] not in ("missing", "dir")
    assert ge._git_config(linked).resolve() == repo / ".git" / "config"
    assert values[ge.GIT_CONFIG] not in ("missing", "irregular")


def test_the_snapshot_name_is_the_one_the_index_reads() -> None:
    assert ge.CONFIG_SNAPSHOT == ri.SNAPSHOT_FILE
    assert LoopConfig().state_file == ri.DEFAULT_STATE_FILE


@pytest.mark.parametrize(
    "snapshot",
    [[], {"bogus": 1}, {"stages": "x"}, {"stages": [{"nope": 1}]}, {"carve_outs": 3}],
)
def test_a_tampered_snapshot_is_a_config_error(snapshot) -> None:
    # Value: protects=a bad snapshot fails closed as a RunError, never a crash;
    # fails_when=from_dict lets a TypeError out; why_new=review testing; seam=none
    from core.config import ConfigError, from_dict

    with pytest.raises(ConfigError):
        from_dict(snapshot)


def test_the_snapshot_round_trips() -> None:
    from core.config import from_dict, to_dict

    assert from_dict(to_dict(DEFAULTS)) == DEFAULTS


def test_setting_an_upstream_leaves_the_git_config_value_alone(make_repo, tmp_path, git) -> None:
    # Value: protects=the ship stage's `git push -u` does not pause every run;
    # fails_when=the whole git config is hashed; why_new=final re-review; seam=none
    repo = make_repo(tmp_path / "main")
    before = ge.guard_map(repo, DEFAULTS, tmp_path)[ge.GIT_CONFIG]
    git(repo, "config", "branch.main.remote", "origin")
    git(repo, "config", "branch.main.merge", "refs/heads/main")
    assert ge.guard_map(repo, DEFAULTS, tmp_path)[ge.GIT_CONFIG] == before
    git(repo, "config", "remote.origin.pushurl", "https://elsewhere/x.git")
    assert ge.guard_map(repo, DEFAULTS, tmp_path)[ge.GIT_CONFIG] != before
    git(repo, "config", "core.hooksPath", "/tmp/hooks")
    git(repo, "config", "include.path", "/tmp/other")
    assert len({ge.guard_map(repo, DEFAULTS, tmp_path)[ge.GIT_CONFIG], before}) == 2
