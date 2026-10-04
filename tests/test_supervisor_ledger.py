"""The supervisor ledger, dedup identities, and single-supervisor lock."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

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


def test_an_orphaned_run_has_its_own_key():
    # Value: protects=the orphaned escalation reaches the operator once a paused or done run
    # loses its worktree; fails_when=pause_key keeps the old pause or done key;
    # why_new=decide escalates orphaned runs first and the ledger suppressed them; seam=none
    paused = {
        "run_id": "R",
        "status": "awaiting_human",
        "paused_reason": "x",
        "paused_prompt_id": "p",
    }
    assert sl.pause_key({**paused, "orphaned": True}) == "R:orphaned"
    prompt = {"outcome": "permission_prompt", "tool_use_id": "t1"}
    assert sl.pause_key({**paused, "orphaned": True}, prompt) == "R:orphaned"
    assert sl.pause_key({"run_id": "R", "status": "done", "orphaned": True}) == "R:orphaned"
    assert sl.pause_key(paused) != "R:orphaned"


# ------------------------------------------------------------------ loop-ledger

LAUNCHER = Path(__file__).resolve().parent.parent / "bin" / "loop-ledger"


def _files(tmp_path: Path) -> tuple[str, str]:
    record = tmp_path / "record.json"
    record.write_text(
        json.dumps(
            {
                "run_id": "R",
                "status": "awaiting_human",
                "paused_reason": "gate",
                "paused_prompt_id": "p1",
            }
        ),
        encoding="utf-8",
    )
    question = tmp_path / "question.json"
    question.write_text(json.dumps({"outcome": "none"}), encoding="utf-8")
    return str(record), str(question)


def _check(record: str, question: str, channel: str, now: datetime) -> list[str]:
    return [
        "--repo",
        "o/r",
        "check",
        "--record-file",
        record,
        "--question-file",
        question,
        "--channel",
        channel,
        "--now",
        now.isoformat(),
    ]


def test_the_command_checks_then_records_one_pause(tmp_path, capsys):
    # Value: protects=a restarted supervisor does not repeat a notification;
    # fails_when=check and record name different keys, or record writes where check
    # does not read; why_new=the routine calls the ledger by name; seam=CLI
    record, question = _files(tmp_path)
    first = _check(record, question, "desktop", NOW)
    assert sl.main(first) == 0
    assert json.loads(capsys.readouterr().out)["key"] == "R:pause:gate:p1"
    recorded = [*first[:2], "record", *first[3:]]
    assert sl.main(recorded) == 0
    assert sl.main(first) == 1
    assert sl.main(_check(record, question, "tracker", NOW)) == 0
    later = NOW + timedelta(seconds=1800)
    assert sl.main(_check(record, question, "desktop", later)) == 0


def test_the_command_refuses_a_malformed_record(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    argv = ["--repo", "o/r", "check", "--record-file", str(bad), "--channel", "desktop"]
    assert sl.main(argv) == 2


def test_the_lock_holds_until_unlock_ends_its_process(capsys):
    # Value: protects=one supervisor per repository across separate shell calls;
    # fails_when=lock returns and frees the lock, or unlock cannot end the holder;
    # why_new=the routine runs lock in the background; seam=CLI
    holder = subprocess.Popen(
        [sys.executable, str(LAUNCHER), "--repo", "o/r", "lock"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout.readline().startswith("LOCKED: pid ")
        assert sl.main(["--repo", "o/r", "lock"]) == 1
        assert "LOCK_HELD" in capsys.readouterr().out
        assert sl.main(["--repo", "o/r", "unlock"]) == 0
        assert holder.wait(timeout=10) == 0
    finally:
        holder.kill()
        holder.stdout.close()
    assert sl.main(["--repo", "o/r", "unlock"]) == 0
    assert "NOT_LOCKED" in capsys.readouterr().out
