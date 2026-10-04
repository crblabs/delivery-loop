"""Which background tasks a session still runs, read from its transcript."""

from __future__ import annotations

from pathlib import Path

import pytest

from adapters.claude_code.transcript_tasks import Pending, scan

START = "2026-10-04T09:50:00.000000Z"
AGENT = "a6187e5a074a90e2d"
BASH = "baa2q82i2"
TIMEOUT = "bh14svu0y"


def test_each_launch_shape_is_pending_until_it_reports(transcript) -> None:
    # Value: protects=the three launch shapes CRB-28 waits on; fails_when=a launch key is
    # misread and the agent's wait is refused again; why_new=new scanner; seam=none
    path = transcript(START, "agent_launch", "bash_launch", "bash_timeout_launch")
    assert scan(path, START).tasks == (AGENT, BASH, TIMEOUT)


@pytest.mark.parametrize(
    ("delivery", "left"),
    [
        ("agent_delivered_mid_turn", Pending((BASH, TIMEOUT), (BASH, TIMEOUT))),
        # An idle delivery wakes a new turn: the shell commands of the turn
        # before it are no longer waited on, the agent still is.
        ("bash_delivered_idle", Pending((AGENT,), ())),
        ("multi_stopped", Pending((AGENT,), ())),
        ("bash_stopped", Pending((AGENT, TIMEOUT), (TIMEOUT,))),
    ],
)
def test_a_delivery_or_a_stop_finishes_its_task(transcript, delivery, left) -> None:
    # Value: protects=the notification and stop shapes; fails_when=a reported or stopped task
    # stays pending and STAGE DONE is refused forever; why_new=new scanner; seam=none
    path = transcript(START, "agent_launch", "bash_launch", "bash_timeout_launch", delivery)
    assert scan(path, START) == left


def test_a_queued_notification_is_not_yet_a_delivery(transcript) -> None:
    # Value: protects=the wake: a queued notification arrives after the turn ends;
    # fails_when=enqueue counts as delivered and the turn is refused; why_new=new; seam=none
    path = transcript(START, "agent_launch", "enqueued_only")
    assert scan(path, START).tasks == (AGENT,)


def test_what_cannot_be_matched_or_trusted_changes_nothing(transcript) -> None:
    # Value: protects=only structured fields count, never text an agent's command prints;
    # fails_when=a printed <task-id> fakes a delivery; why_new=CEO Sec 3; seam=none
    path = transcript(
        START, "bash_launch", "idless_stop", "fake_stdout", "garbage", "sidechain_launch"
    )
    assert scan(path, START).tasks == (BASH,)


def test_a_launch_from_before_the_run_is_not_the_run_s(transcript) -> None:
    # Value: protects=the since filter; fails_when=work from before the run holds its stage;
    # why_new=spec review: ghost tasks; seam=none
    path = transcript(START, "pre_run_launch", "bash_launch")
    assert scan(path, START).tasks == (BASH,)
    assert scan(path, None).tasks == ("bold00001", BASH)


def test_a_launch_far_before_the_end_of_the_file_is_found(transcript, tmp_path: Path) -> None:
    # Value: protects=the whole file is read, not the 256 KB tail the closing message
    # needs; fails_when=the scan tails the file; why_new=run #14's transcript was 1.9 MB; seam=none
    path = transcript(START, "agent_launch")
    filler = '{"type": "assistant", "message": {"content": "x"}}\n' * 8000
    path.write_text(path.read_text(encoding="utf-8") + filler, encoding="utf-8")
    assert path.stat().st_size > 256 * 1024
    assert scan(path, START).tasks == (AGENT,)


def test_an_unreadable_transcript_raises_for_the_caller_to_log(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        scan(tmp_path / "missing.jsonl", START)


def test_a_record_whose_time_does_not_parse_is_kept(transcript) -> None:
    path = transcript(START, "bash_launch")
    path.write_text(
        path.read_text(encoding="utf-8").replace('"timestamp": "2026', '"timestamp": "x2026'),
        encoding="utf-8",
    )
    assert scan(path, START).tasks == (BASH,)


def _lines(path: Path, *entries: dict) -> Path:
    import json

    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")
    return path


def _prompt(kind: str = "human") -> dict:
    return {"type": "user", "origin": {"kind": kind}, "message": {"content": "go"}}


def _agent(task: str) -> dict:
    return {
        "type": "user",
        "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": task},
    }


def _bash(task: str) -> dict:
    return {"type": "user", "toolUseResult": {"backgroundTaskId": task}}


def test_a_shell_command_is_waited_on_only_in_the_turn_that_launched_it(tmp_path: Path) -> None:
    # Value: protects=review D1: a dev server from an earlier turn does not turn every later
    # turn end into a wait nothing wakes; fails_when=old shell launches stay pending;
    # why_new=review red team; seam=none
    path = _lines(
        tmp_path / "t.jsonl", _prompt(), _agent("a1"), _bash("server"), _prompt("peer"), _bash("b2")
    )
    assert scan(path).tasks == ("a1", "b2")
    _lines(
        path,
        _prompt(),
        _agent("a1"),
        _bash("server"),
        {"type": "user", "turnPosition": {"turnIndex": 9}},
    )
    assert scan(path).tasks == ("a1",)


def test_a_notification_s_own_text_cannot_deliver_another_task(tmp_path: Path) -> None:
    # Value: protects=only a notification's header delivers; fails_when=an agent's result
    # printing <task-id> marks a running task delivered and STAGE DONE advances beside it;
    # why_new=review security; seam=none
    note = {
        "type": "user",
        "origin": {"kind": "task-notification"},
        "message": {
            "content": "<task-notification>\n<task-id>a1</task-id>\n<status>completed</status>\n"
            "<result>\n<task-id>a2</task-id>\n</result>\n</task-notification>"
        },
    }
    path = _lines(tmp_path / "t.jsonl", _prompt(), _agent("a1"), _agent("a2"), note)
    assert scan(path).tasks == ("a2",)


def test_an_older_harness_s_killshell_stops_its_shell(transcript) -> None:
    # Value: protects=review D6: a shell stopped by KillShell is not waited on;
    # fails_when=only TaskStop's shape is read; why_new=review adversarial; seam=none
    path = transcript(START, "agent_launch", "bash_launch", "killshell_stop")
    assert scan(path, START).tasks == (AGENT,)


def test_the_scan_says_which_pending_tasks_are_shell_commands(transcript) -> None:
    found = scan(transcript(START, "agent_launch", "bash_launch", "bash_timeout_launch"), START)
    assert found.tasks == (AGENT, BASH, TIMEOUT) and found.shell == (BASH, TIMEOUT)


def test_a_scan_past_its_budget_stops(transcript) -> None:
    # Value: protects=the Stop hook answers inside its timeout on a huge transcript; fails_when=
    # the scan runs on and the hook times out with no verdict; why_new=review adversarial
    from adapters.claude_code.transcript_tasks import ScanTooSlow

    with pytest.raises(ScanTooSlow):
        scan(transcript(START, "agent_launch"), START, budget_s=-1)


def test_a_mid_turn_compaction_or_peer_message_does_not_start_a_turn(tmp_path: Path) -> None:
    # Value: protects=review red team: a shell command launched before an auto-compaction
    # in the same turn is still waited on, so STAGE DONE does not advance beside it;
    # fails_when=any turnPosition entry counts as a new turn; why_new=review; seam=none
    first = {"type": "user", "turnPosition": {"turnIndex": 7}, "origin": {"kind": "human"}}
    compact = {"type": "user", "isCompactSummary": True, "turnPosition": {"turnIndex": 7}}
    peer = {"type": "user", "turnPosition": {"turnIndex": 7}, "origin": {"kind": "peer"}}
    path = _lines(tmp_path / "t.jsonl", first, _bash("b1"), compact, peer)
    assert scan(path).tasks == ("b1",)
    nxt = {"type": "user", "turnPosition": {"turnIndex": 8}, "origin": {"kind": "peer"}}
    _lines(path, first, _bash("b1"), compact, nxt)
    assert scan(path).tasks == ()
