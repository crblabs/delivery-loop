"""The supervisor ledger, dedup identities, and single-supervisor lock."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core import supervisor_ledger as sl

NOW = datetime(2026, 9, 16, 10, 0, 0, tzinfo=UTC)


def test_pause_key_awaiting_human():
    rec = {
        "run_id": "R",
        "status": "awaiting_human",
        "paused_reason": "guard_changed",
        "paused_prompt_id": "p1",
    }
    assert sl.pause_key(rec) == "R:pause:guard_changed:p1"


def test_pause_key_terminal_identities_differ():
    assert sl.pause_key({"run_id": "R", "status": "failed"}) == "R:failed"
    assert sl.pause_key({"run_id": "R", "status": "done"}) == "R:done"


def test_pause_key_hidden_gate_uses_tool_id():
    rec = {"run_id": "R", "status": "running"}
    q = {"outcome": "pending", "tool_use_id": "toolu_9"}
    assert sl.pause_key(rec, q) == "R:gate:toolu_9"


def test_pause_key_permission_prompt_uses_tool_id():
    rec = {"run_id": "R", "status": "running"}
    q = {"outcome": "permission_prompt", "tool_use_id": "toolu_b"}
    assert sl.pause_key(rec, q) == "R:permprompt:toolu_b"


def test_pause_key_permission_prompt_on_done_is_not_the_done_key():
    # A prompt on a done run must not collapse into the run's ":done" key, or the
    # already-sent done notification would suppress it forever.
    rec = {"run_id": "R", "status": "done"}
    q = {"outcome": "permission_prompt", "tool_use_id": "toolu_b"}
    assert sl.pause_key(rec, q) == "R:permprompt:toolu_b"
    assert sl.pause_key(rec) == "R:done"
    assert sl.pause_key(rec, q) != sl.pause_key(rec)


def test_should_notify_first_time_true():
    assert sl.should_notify([], "R:pause:x:p1", "desktop", NOW, 1800) is True


def test_should_notify_dedups_within_window(tmp_path):
    path = sl.ledger_path("slug", home=tmp_path)
    sl.append_notification(path, "R:pause:x:p1", "desktop", NOW)
    entries = sl.read_ledger(path)
    soon = NOW + timedelta(seconds=100)
    assert sl.should_notify(entries, "R:pause:x:p1", "desktop", soon, 1800) is False
    later = NOW + timedelta(seconds=2000)
    assert sl.should_notify(entries, "R:pause:x:p1", "desktop", later, 1800) is True


def test_channels_are_independent(tmp_path):
    path = sl.ledger_path("slug", home=tmp_path)
    sl.append_notification(path, "R:pause:x:p1", "desktop", NOW)
    entries = sl.read_ledger(path)
    assert sl.should_notify(entries, "R:pause:x:p1", "linear", NOW, 1800) is True


def test_done_never_re_notified(tmp_path):
    path = sl.ledger_path("slug", home=tmp_path)
    sl.append_notification(path, "R:done", "desktop", NOW)
    entries = sl.read_ledger(path)
    far = NOW + timedelta(days=30)
    assert sl.should_notify(entries, "R:done", "desktop", far, 1800) is False


def test_lock_refuses_a_second_supervisor(tmp_path):
    first = sl.acquire_lock("slug", home=tmp_path)
    assert first is not None
    second = sl.acquire_lock("slug", home=tmp_path)
    assert second is None


def test_append_is_one_line_each(tmp_path):
    path = sl.ledger_path("slug", home=tmp_path)
    sl.append_notification(path, "R:pause:x:p1", "desktop", NOW)
    sl.append_notification(path, "R:pause:x:p1", "linear", NOW)
    assert len(path.read_text().strip().splitlines()) == 2


def test_append_after_torn_tail_keeps_new_entry(tmp_path):
    path = sl.ledger_path("slug", home=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # A crash left the last line without a trailing newline.
    path.write_text(
        '{"key":"R:pause:x:p1","channel":"desktop","ts":"2026-09-16T10:00:00+00:00"',
        encoding="utf-8",
    )
    sl.append_notification(path, "R:done", "desktop", NOW)
    entries = sl.read_ledger(path)
    # The torn line is dropped, but the new entry survives on its own line.
    assert any(e.get("key") == "R:done" for e in entries)


def test_lock_refuses_a_symlinked_lock_file(tmp_path):
    import os

    target = tmp_path / "victim.txt"
    target.write_text("do not truncate me", encoding="utf-8")
    lock = sl.ledger_path("slug", home=tmp_path).with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, lock)
    assert sl.acquire_lock("slug", home=tmp_path) is None
    assert target.read_text() == "do not truncate me"


def test_append_refuses_a_symlinked_ledger(tmp_path):
    import os

    target = tmp_path / "victim.txt"
    target.write_text("run state", encoding="utf-8")
    path = sl.ledger_path("slug", home=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, path)
    with pytest.raises(OSError):
        sl.append_notification(path, "R:done", "desktop", NOW)
    assert target.read_text() == "run state"


@pytest.mark.parametrize("bad", ["../../outside", "a/b", "", "..", "with space"])
def test_unsafe_slug_refused(bad):
    with pytest.raises(ValueError):
        sl.ledger_path(bad)


def test_read_ledger_tolerates_garbage(tmp_path):
    path = sl.ledger_path("slug", home=tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        b'null\n{"key":"R:done","channel":"desktop","ts":"2026-09-16T10:00:00+00:00"}\n\xff\xfe\n'
    )
    entries = sl.read_ledger(path)
    assert entries == []  # invalid utf-8 makes the whole read tolerant-empty


def test_pause_key_falls_back_to_worktree_when_unobservable():
    a = sl.pause_key({"worktree": "/repo/a", "condition": "corrupt"})
    b = sl.pause_key({"worktree": "/repo/b", "condition": "corrupt"})
    assert a != b
