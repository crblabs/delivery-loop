"""loop-prune: list the runs whose worktree is gone, and delete them on request."""

from __future__ import annotations

import json
from pathlib import Path

from core import pipeline_state as ps
from core import supervisor_prune as sp

REPO = "crblabs/delivery-loop"


def run_in(worktree: Path) -> Path:
    worktree.mkdir(parents=True)
    path = ps.prepare_run_dir(worktree, REPO)
    path.write_text("{}", encoding="utf-8")
    return path.parent


def test_lists_only_the_runs_whose_worktree_is_gone(tmp_path, capsys):
    live = run_in(tmp_path / "live")
    gone = run_in(tmp_path / "gone")
    (tmp_path / "gone").rmdir()
    assert sp.main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert [entry["run_dir"] for entry in report] == [str(gone)]
    assert report[0]["deleted"] is False
    assert gone.exists() and live.exists()


def test_yes_deletes_the_orphaned_runs_and_nothing_else(tmp_path, capsys):
    live = run_in(tmp_path / "live")
    gone = run_in(tmp_path / "gone")
    unrecorded = run_in(tmp_path / "unrecorded")
    (unrecorded / ps.WORKTREE_FILE).unlink()
    (tmp_path / "gone").rmdir()
    assert sp.main(["--yes"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["deleted"] is True
    assert not gone.exists()
    assert live.exists() and unrecorded.exists()


def test_a_refused_config_exits_2(tmp_path, capsys):
    bad = tmp_path / "loop.toml"
    bad.write_text('[harness]\nstate_root = "relative"\n', encoding="utf-8")
    assert sp.main(["--config", str(bad)]) == 2
    assert "PRUNE_CONFIG_INVALID" in capsys.readouterr().err


def test_a_bad_user_file_stops_prune(tmp_path, monkeypatch, git, capsys):
    # Value: protects=loop-prune reads the same config as loop-scan and stops on a bad file;
    # fails_when=prune ignores the config lookup without --config; why_new=prune tests named
    # --config or used none; seam=none
    from core.config import USER_CONFIG_DIR

    repo = tmp_path / "mine"
    repo.mkdir()
    git(repo, "init", "-q")
    user = Path.home() / USER_CONFIG_DIR / "mine.toml"
    user.parent.mkdir(parents=True)
    user.write_text("[harness\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    assert sp.main([]) == 2
    assert "PRUNE_CONFIG_INVALID" in capsys.readouterr().err
