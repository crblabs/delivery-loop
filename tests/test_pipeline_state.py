"""The supervisor's state reader validates every field it exposes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import pipeline_state as ps


def make_state(**over) -> dict:
    state = {
        "version": 1,
        "run_id": "9f1c2e7a-3b4d-4e5f-8a6b-7c8d9e0f1a2b",
        "revision": 3,
        "stages": list(ps.STAGES),
        "current": 0,
        "current_stage": "autoplan",
        "status": "running",
        "attempts": {s: 0 for s in ps.STAGES},
        "total_attempts": 0,
        "session_id": None,
        "updated_at": "2026-09-16T09:42:11.000000+00:00",
        "history": [],
    }
    state.update(over)
    return state


def write(tmp_path: Path, state) -> Path:
    path = ps.state_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


def test_ok(tmp_path):
    path = write(tmp_path, make_state())
    condition, state = ps.read_state(path)
    assert condition == "ok"
    assert state["run_id"] == "9f1c2e7a-3b4d-4e5f-8a6b-7c8d9e0f1a2b"


def test_missing(tmp_path):
    assert ps.read_state(ps.state_path(tmp_path)) == ("missing", None)


def test_unreadable_is_a_directory(tmp_path):
    path = ps.state_path(tmp_path)
    path.mkdir(parents=True)
    assert ps.read_state(path)[0] == "unreadable"


def test_corrupt_bad_json(tmp_path):
    path = ps.state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    assert ps.read_state(path)[0] == "corrupt"


def test_corrupt_bad_schema(tmp_path):
    path = write(tmp_path, make_state(status="banana"))
    assert ps.read_state(path)[0] == "corrupt"


def test_unsupported_version(tmp_path):
    path = write(tmp_path, make_state(version=2))
    assert ps.read_state(path)[0] == "unsupported"


def test_huge_int_literal_is_corrupt_not_crash(tmp_path):
    # A 5000-digit int raises ValueError inside json, not JSONDecodeError.
    path = ps.state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"version":' + "9" * 5000 + "}", encoding="utf-8")
    assert ps.read_state(path)[0] == "corrupt"


@pytest.mark.parametrize(
    "value,ok",
    [
        ("2026-09-16T09:42:11+00:00", True),
        ("2026-09-16T09:42:11Z", True),
        ("2026-09-16T09:42:11", False),
        ("not-a-date", False),
        (None, False),
    ],
)
def test_parse_iso(value, ok):
    assert (ps.parse_iso(value) is not None) is ok
