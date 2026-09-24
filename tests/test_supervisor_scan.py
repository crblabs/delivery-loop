"""The supervisor scanner: nested discovery, conditions, exit codes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

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


def worktree(container: Path, name: str, state=None, raw=None) -> Path:
    wt = container / f"repo-{name}" / f"task-{name}"
    (wt / ".claude").mkdir(parents=True)
    path = ps.state_path(wt)
    if raw is not None:
        path.write_text(raw, encoding="utf-8")
    elif state is not None:
        path.write_text(json.dumps(state), encoding="utf-8")
    return wt


def glob_for(container: Path) -> str:
    return str(container / "*" / "*" / ".claude" / "pipeline.local.json")


def test_discovers_nested_worktrees(tmp_path):
    worktree(tmp_path, "one", make_state())
    worktree(tmp_path, "two", make_state())
    records = ss.scan(glob_for(tmp_path), None, NOW, 600)
    assert len(records) == 2
    assert {r["condition"] for r in records} == {"ok"}


def test_shallow_glob_matches_nothing(tmp_path):
    worktree(tmp_path, "one", make_state())
    shallow = str(tmp_path / "*" / ".claude" / "pipeline.local.json")
    assert ss.discover(shallow) == []


def test_conditions_reported_not_crashed(tmp_path):
    worktree(tmp_path, "ok", make_state())
    worktree(tmp_path, "bad", raw="{broken")
    records = {r["worktree"]: r for r in ss.scan(glob_for(tmp_path), None, NOW, 600)}
    conditions = sorted(r["condition"] for r in records.values())
    assert conditions == ["corrupt", "ok"]


def test_repo_filter_excludes_other_repos(tmp_path):
    worktree(tmp_path, "mine", make_state())
    worktree(tmp_path, "other", make_state(repo="someone/else"))
    records = ss.scan(glob_for(tmp_path), "crblabs/delivery-loop", NOW, 600)
    assert [r["repo"] for r in records] == ["crblabs/delivery-loop"]


def test_age_and_stale(tmp_path):
    worktree(tmp_path, "fresh", make_state(updated_at="2026-09-16T09:59:30+00:00"))
    worktree(tmp_path, "old", make_state(updated_at="2026-09-16T09:00:00+00:00"))
    recs = {Path(r["worktree"]).name: r for r in ss.scan(glob_for(tmp_path), None, NOW, 600)}
    assert recs["task-fresh"]["is_stale"] is False
    assert recs["task-old"]["is_stale"] is True
    assert recs["task-old"]["age_seconds"] == 3600.0


def test_non_utc_updated_at(tmp_path):
    # 09:00+02:00 is 07:00 UTC; against 10:00 UTC that is 3 hours old.
    worktree(tmp_path, "tz", make_state(updated_at="2026-09-16T09:00:00+02:00"))
    rec = ss.scan(glob_for(tmp_path), None, NOW, 600)[0]
    assert rec["age_seconds"] == 10800.0


def test_exit_codes(tmp_path, capsys):
    empty = str(tmp_path / "none" / "*" / "*" / ".claude" / "pipeline.local.json")
    assert ss.main(["--worktrees-glob", empty]) == 2
    worktree(tmp_path, "run", make_state(status="running", updated_at="2026-09-16T09:59:30+00:00"))
    code = ss.main(["--worktrees-glob", glob_for(tmp_path), "--now", NOW.isoformat()])
    assert code == 0
    worktree(tmp_path, "pause", make_state(status="awaiting_human"))
    code = ss.main(["--worktrees-glob", glob_for(tmp_path), "--now", NOW.isoformat()])
    assert code == 1
    capsys.readouterr()
