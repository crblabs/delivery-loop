"""Which background tasks of a session are still running, from its transcript.

The Stop hook lets a turn end while a background task runs, so the task's
notification can wake the session. Claude Code does not say in the Stop payload
which tasks run, but its session transcript records each one:

  * a launch: a tool result whose ``toolUseResult`` holds ``isAsync: true``,
    ``status: "async_launched"`` and an ``agentId`` (a background agent), or a
    ``backgroundTaskId`` (a background shell command, including a foreground
    one the harness moved to the background when it passed its timeout);
  * a delivery: the task's notification reached the session, as a
    ``queued_command`` attachment (mid-turn) or a ``user`` entry whose
    ``origin.kind`` is ``task-notification`` (the wake), each holding one or
    more ``<task-id>`` lines; the launch id is the notification's task id;
  * a stop: a tool result whose ``toolUseResult`` holds a ``task_id`` and a
    "Successfully stopped task" message (``TaskStop``), or a ``shell_id`` and a
    "Successfully killed shell" message (``KillShell`` in older harnesses),
    which leaves no notification.

A task is pending when its launch has neither a delivery nor a stop. Only those
structured fields count, never the text of a tool result, which an agent's own
command can print. Within a notification only its header counts: the task-id
lines before its status, summary or result, since an agent's result is its own
text. A queued but undelivered notification (``enqueue``) is not a delivery:
the harness delivers it after the turn ends, which is the wake.

A background shell command is pending only in the turn that launched it. One
that never ends on its own, such as a dev server, would otherwise turn every
later turn end into a wait that nothing wakes. A turn starts at a prompt: a
``user`` entry whose ``turnPosition`` names a new ``turnIndex`` (a mid-turn
compaction or peer message repeats the turn's), or, without one, whose
``origin`` is of kind ``human``, ``peer`` or ``task-notification``. A
background agent is pending until it reports, whichever turn launched it: it
always reports.

The whole file is read, line by line: a launch can sit far before the tail the
Stop hook reads for the closing message. A line without one of the marker
strings is skipped before it is decoded, and a scan stops at a time budget.
This module, ``supervisor_transcript`` and the closing-message reader in
``hooks`` know the transcript layout.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from core import pipeline_state as ps

# Bytes, so a line without one is skipped before it is decoded.
_MARKERS = (
    b"async_launched",
    b"backgroundTaskId",
    b"<task-id>",
    b"Successfully stopped task",
    b"Successfully killed shell",
    b'"turnPosition"',
    b'"origin"',
)
# How long a scan may take, well inside the Stop hook's timeout, and how often
# it looks at the clock.
SCAN_BUDGET_S = 10.0
_CLOCK_EVERY_BYTES = 1 << 20
_TASK_ID_RE = re.compile(r"^\s*<task-id>\s*([^<\s]+)\s*</task-id>\s*$", re.MULTILINE)
# Where a notification's header ends: the agent's own text can follow.
_HEADER_END_RE = re.compile(r"<(?:status|summary|result|output)\b")
_NOTIFICATION = "task-notification"
_TURN_ORIGINS = ("human", "peer", _NOTIFICATION)


def _launch_id(result: dict) -> tuple[str, bool] | None:
    """A launch's task id, and whether it is an agent's, or None."""
    if result.get("isAsync") is True and result.get("status") == "async_launched":
        agent = result.get("agentId")
        return (agent, True) if isinstance(agent, str) and agent else None
    task = result.get("backgroundTaskId")
    return (task, False) if isinstance(task, str) and task else None


def _turn_of(entry: dict) -> tuple[str, object] | None:
    """The turn a prompt entry belongs to, or None for any other entry.

    A ``turnPosition`` names its turn: a compaction or a peer message in the
    middle of a turn repeats the turn's ``turnIndex`` and starts nothing. An
    entry without one (a wake by a task notification) starts a turn by its
    ``origin``, each such entry its own.
    """
    if entry.get("type") != "user" or "toolUseResult" in entry:
        return None
    position = entry.get("turnPosition")
    if isinstance(position, dict) and "turnIndex" in position:
        return ("index", position.get("turnIndex"))
    origin = entry.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return ("origin", object()) if kind in _TURN_ORIGINS else None


# A stop's id field and the start of its message: TaskStop, and the KillShell
# of older harnesses (its shape is from their docs, not a local transcript).
_STOPS = (("task_id", "Successfully stopped task"), ("shell_id", "Successfully killed shell"))


def _stopped_id(result: dict) -> str | None:
    message = result.get("message")
    if not isinstance(message, str):
        return None
    for field, prefix in _STOPS:
        task = result.get(field)
        if isinstance(task, str) and task and message.startswith(prefix):
            return task
    return None


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    return ""


def _header_ids(text: str) -> list[str]:
    end = _HEADER_END_RE.search(text)
    return _TASK_ID_RE.findall(text[: end.start()] if end else text)


def _delivered_ids(entry: dict) -> list[str]:
    """The task ids a notification record delivers, or none."""
    attachment = entry.get("attachment")
    if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
        if attachment.get("commandMode") == _NOTIFICATION:
            prompt = attachment.get("prompt")
            return _header_ids(prompt) if isinstance(prompt, str) else []
        return []
    origin = entry.get("origin")
    if entry.get("type") != "user" or not isinstance(origin, dict):
        return []
    if origin.get("kind") != _NOTIFICATION:
        return []
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return _header_ids(_content_text(content))


def _before(entry: dict, since: datetime | None) -> bool:
    """Whether a record is older than the run, so not this run's work.

    A record whose time cannot be read is kept: a wait too many is capped and
    released by a person, a wait too few loses the task's result.
    """
    if since is None:
        return False
    at = ps.parse_iso(entry.get("timestamp"))
    return at is not None and at < since


class ScanTooSlow(OSError):
    """The transcript took longer than the scan's budget to read."""


@dataclass(frozen=True)
class Pending:
    """The tasks still running, in launch order, and which of them are shell commands."""

    tasks: tuple[str, ...]
    shell: tuple[str, ...]


def scan(path: Path, since: str | None = None, budget_s: float = SCAN_BUDGET_S) -> Pending:
    """The background tasks launched at or after ``since`` that have neither
    reported nor been stopped: every agent, and the shell commands of the last
    turn.

    Raises ``OSError`` when the file cannot be read, and ``ScanTooSlow`` past
    ``budget_s``: the caller logs either and treats it as no task, so a turn is
    judged as it always was rather than the hook timing out with no verdict.
    """
    floor = ps.parse_iso(since) if since else None
    # Each launch's turn number; an agent's is None, pending in any turn.
    launched: dict[str, int | None] = {}
    finished: set[str] = set()
    turn = 0
    last_turn: tuple[str, object] | None = None
    deadline = time.monotonic() + budget_s
    unclocked = 0
    with path.open("rb") as handle:
        for line in handle:
            # By bytes, not lines: a transcript line is often kilobytes.
            unclocked += len(line)
            if unclocked >= _CLOCK_EVERY_BYTES:
                unclocked = 0
                if time.monotonic() > deadline:
                    raise ScanTooSlow(f"the transcript scan passed {budget_s:g} s")
            if not any(marker in line for marker in _MARKERS):
                continue
            # Every decoded line too: one can be megabytes of tool output.
            if time.monotonic() > deadline:
                raise ScanTooSlow(f"the transcript scan passed {budget_s:g} s")
            try:
                entry = json.loads(line)
            except (ValueError, RecursionError):
                continue
            if not isinstance(entry, dict) or entry.get("isSidechain") is True:
                continue
            found = _turn_of(entry)
            if found is not None and found != last_turn:
                turn += 1
                last_turn = found
            result = entry.get("toolUseResult")
            if isinstance(result, dict):
                launch = _launch_id(result)
                if launch is not None and not _before(entry, floor):
                    task, agent = launch
                    launched[task] = None if agent else turn
                stopped = _stopped_id(result)
                if stopped is not None:
                    finished.add(stopped)
                continue
            finished.update(_delivered_ids(entry))
    left = [(t, at) for t, at in launched.items() if t not in finished and at in (None, turn)]
    return Pending(tuple(t for t, _ in left), tuple(t for t, at in left if at is not None))
