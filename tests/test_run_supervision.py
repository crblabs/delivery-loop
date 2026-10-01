"""The supervisor on runs the plugin drives: a lost index entry, and pruning."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime

from core import run_index as ri
from core import supervisor_decide as sd
from core import supervisor_prune as sp
from core import supervisor_scan as ss
from core.config import DEFAULTS


def _scan() -> list[dict]:
    return ss.scan(None, datetime.now(UTC), 600, DEFAULTS, per_worktree=True)


def test_an_active_run_without_its_index_entry_needs_a_person(start_run) -> None:
    # Value: protects=a deleted index entry, which silences the hooks, reaches a person;
    # fails_when=the scan reads the state alone; why_new=CEO section 3; seam=none
    run, worktree = start_run()
    ri.remove_entry(run.key, "s1")
    (record,) = _scan()
    assert record["index_missing"] is True
    assert ss._needs_attention(record)
    assert sd.decide(record)["reason"] == "index_missing"


def test_a_finished_run_without_an_index_entry_is_fine(start_run) -> None:
    run, _ = start_run()
    ri.abort_run(run.key, run.entry, "person")
    (record,) = _scan()
    assert record["index_missing"] is False
    assert sd.decide(record)["reason"] == "failed"


def test_a_run_an_older_install_started_is_not_flagged(start_run) -> None:
    run, _ = start_run()
    state = json.loads(run.state_path.read_text(encoding="utf-8"))
    del state["driver"]
    run.state_path.write_text(json.dumps(state), encoding="utf-8")
    ri.remove_entry(run.key, "s1")
    (record,) = _scan()
    assert record["index_missing"] is False


def test_prune_removes_the_index_entries_of_a_deleted_run(start_run, capsys) -> None:
    # Value: protects=a worktree recreated at the same path can start again;
    # fails_when=prune leaves the index naming a deleted run; why_new=eng E5; seam=none
    run, worktree = start_run()
    shutil.rmtree(worktree)
    assert sp.main(["--yes"]) == 0
    (entry,) = json.loads(capsys.readouterr().out)
    assert entry["deleted"] is True
    assert entry["index_entries_removed"] == 2
    assert ri.read_entry(ri.index_path(run.key)) is None
    assert ri.read_entry(ri.session_path("s1")) is None


def test_a_plugin_run_is_scanned_under_the_config_it_started_with(start_run) -> None:
    # Value: protects=a run started with --config, or a loop.toml edited mid-run, reads as
    # ok to the supervisor as it does to the hooks; fails_when=the scan reloads loop.toml;
    # why_new=final re-review; seam=none
    from core.config import LoopConfig, StageSpec

    config = LoopConfig(stages=(StageSpec(name="implement"), StageSpec(name="review")))
    run, worktree = start_run(config=config)
    (worktree / "loop.toml").write_text('[[stages]]\nname = "ship"\n', encoding="utf-8")
    (record,) = _scan()
    assert record["condition"] == "ok"
    assert record["stage_count"] == 2


def test_a_plugin_run_with_an_unreadable_snapshot_needs_a_person(start_run) -> None:
    # Value: protects=a tampered config snapshot reaches a person instead of being read
    # under loop.toml; fails_when=_snapshot treats an unreadable file as no snapshot;
    # why_new=the snapshot tests cover a valid snapshot only; seam=none
    run, _ = start_run()
    (run.run_dir / ri.SNAPSHOT_FILE).write_text("{", encoding="utf-8")
    (record,) = _scan()
    assert record["condition"] == "config_invalid"
    assert ss._needs_attention(record)
    assert sd.decide(record)["action"] != "noop"
