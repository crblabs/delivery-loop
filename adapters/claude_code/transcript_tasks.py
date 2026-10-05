"""Which background tasks of a session are still running, from its transcript.

The Stop hook lets a turn end while a background task runs, so the task's
notification can wake the session. Claude Code does not say in the Stop payload
which tasks run, but its session transcript records each one:

  * a launch: a tool result whose ``toolUseResult`` holds ``isAsync: true``,
    ``status: "async_launched"`` and an ``agentId`` (a background agent), a
    ``backgroundTaskId`` (a background shell command, including a foreground
    one the harness moved to the background when it passed its timeout), a
    ``taskId`` with a ``timeoutMs`` (a Monitor), or a ``resumedAgentId`` (an
    agent ``SendMessage`` woke again, pending until its next report);
  * a delivery: the task's notification reached the session, as a
    ``queued_command`` attachment (mid-turn) or a ``user`` entry whose
    ``origin.kind`` is ``task-notification`` (the wake), each holding one or
    more ``<task-id>`` lines; the launch id is the notification's task id.
    Only a notification with a ``<status>`` ends a task: a Monitor's events
    come without one, and an agent that stopped with background work of its
    own still running says so in its ``<note>`` and reports again later;
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

A background shell command is pending only in the turn that launched it, and a
CI watch among them (``core.guard.ci_watch``, read from the launching tool
call) is not counted as a shell-only wait. One
that never ends on its own, such as a dev server, would otherwise turn every
later turn end into a wait that nothing wakes. A turn starts at a prompt: a
``user`` entry whose ``turnPosition`` names a new ``turnIndex`` (a mid-turn
compaction or peer message repeats the turn's), or, without one, whose
``origin`` is of kind ``human``, ``peer`` or ``task-notification``. A
background agent is pending until it reports, whichever turn launched it: it
always reports.

The file is read line by line: a launch can sit far before the tail the Stop
hook reads for the closing message. A line without one of the marker strings
is skipped before it is decoded, and a scan stops at a time budget. With a
cache file the scan keeps its state and reads only the bytes added since the
last Stop; a transcript that was rewritten or truncated is read again whole.
This module, ``supervisor_transcript`` and the closing-message reader in
``hooks`` know the transcript layout.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from core import guard
from core import pipeline_state as ps

# Bytes, so a line without one is skipped before it is decoded.
_MARKERS = (
    b"async_launched",
    b"backgroundTaskId",
    b"resumedAgentId",
    b'"timeoutMs"',
    b"<task-id>",
    b"Successfully stopped task",
    b"Successfully killed shell",
    b'"run_in_background"',
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
_STATUS_RE = re.compile(r"<status>\s*[^<\s]+\s*</status>")
_NOTE_RE = re.compile(r"<note>([^<]*)</note>")
# The harness's note on an agent that stopped while its own background work
# still runs: it reports again when that work is done.
_STILL_RUNNING = "background work of its own still running"
_NOTIFICATION = "task-notification"
_TURN_ORIGINS = ("human", "peer", _NOTIFICATION)
# How much of the file's start identifies it for the cache.
_HEAD_BYTES = 4096
_CACHE_VERSION = 1


def _launch_id(result: dict) -> tuple[str, bool] | None:
    """A launch's task id, and whether it is pending in any turn (an agent or
    a Monitor) rather than only in its own (a shell command), or None."""
    if result.get("isAsync") is True and result.get("status") == "async_launched":
        agent = result.get("agentId")
        return (agent, True) if isinstance(agent, str) and agent else None
    resumed = result.get("resumedAgentId")
    if isinstance(resumed, str) and resumed and result.get("success") is True:
        return (resumed, True)
    monitor = result.get("taskId")
    if isinstance(monitor, str) and monitor and "timeoutMs" in result:
        return (monitor, True)
    task = result.get("backgroundTaskId")
    return (task, False) if isinstance(task, str) and task else None


def _turn_of(entry: dict, at: int) -> list | None:
    """The turn a prompt entry belongs to, or None for any other entry.

    A ``turnPosition`` names its turn: a compaction or a peer message in the
    middle of a turn repeats the turn's ``turnIndex`` and starts nothing. An
    entry without one (a wake by a task notification) starts a turn by its
    ``origin``, each such entry its own (named by its byte offset ``at``).
    """
    if entry.get("type") != "user" or "toolUseResult" in entry:
        return None
    position = entry.get("turnPosition")
    if isinstance(position, dict) and "turnIndex" in position:
        return ["index", position.get("turnIndex")]
    origin = entry.get("origin")
    kind = origin.get("kind") if isinstance(origin, dict) else None
    return ["origin", at] if kind in _TURN_ORIGINS else None


# A stop's id field and the start of its message: TaskStop, and the KillShell
# of older harnesses (its shape is from their docs, not a local transcript).
_STOPS = (("task_id", "Successfully stopped task"), ("shell_id", "Successfully killed shell"))


def _stopped_id(result: dict) -> str | None:
    message = result.get("message")
    if not isinstance(message, str):
        return None
    for field_name, prefix in _STOPS:
        task = result.get(field_name)
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
    """The ids a notification ends: its header's, when it carries a status and
    no note that the task still has work running."""
    own = text.split("<result", 1)[0]
    if not _STATUS_RE.search(own):
        return []
    note = _NOTE_RE.search(own)
    if note and _STILL_RUNNING in note.group(1):
        return []
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


def _background_commands(entry: dict) -> dict[str, str]:
    """The background shell commands an assistant entry calls, by tool-use id."""
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    found: dict[str, str] = {}
    if entry.get("type") != "assistant" or not isinstance(content, list):
        return found
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        call = block.get("input")
        if (
            block.get("name") == "Bash"
            and isinstance(call, dict)
            and call.get("run_in_background") is True
            and isinstance(call.get("command"), str)
            and isinstance(block.get("id"), str)
        ):
            found[block["id"]] = call["command"]
    return found


def _result_tool_use_id(entry: dict) -> str | None:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                use = block.get("tool_use_id")
                return use if isinstance(use, str) else None
    return None


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


@dataclass
class _State:
    """What a scan has read so far, so a later one can go on from ``offset``."""

    since: str | None = None
    offset: int = 0
    head: str = ""
    inode: int = 0
    # Each launch's turn number; an agent's or a Monitor's is None (any turn).
    launched: dict[str, int | None] = field(default_factory=dict)
    finished: set[str] = field(default_factory=set)
    # Background shell commands still to be matched to their launch, and the
    # launches that are CI watches.
    commands: dict[str, str] = field(default_factory=dict)
    watches: set[str] = field(default_factory=set)
    turn: int = 0
    last_turn: list | None = None

    def to_json(self) -> dict:
        return {
            "v": _CACHE_VERSION,
            "since": self.since,
            "offset": self.offset,
            "head": self.head,
            "inode": self.inode,
            "launched": self.launched,
            "finished": sorted(self.finished),
            "commands": self.commands,
            "watches": sorted(self.watches),
            "turn": self.turn,
            "last_turn": self.last_turn,
        }

    @classmethod
    def from_json(cls, data: object) -> _State | None:
        if not isinstance(data, dict) or data.get("v") != _CACHE_VERSION:
            return None
        try:
            return cls(
                since=data["since"],
                offset=int(data["offset"]),
                head=str(data["head"]),
                inode=int(data["inode"]),
                launched={str(k): v for k, v in dict(data["launched"]).items()},
                finished=set(data["finished"]),
                commands={str(k): str(v) for k, v in dict(data["commands"]).items()},
                watches=set(data["watches"]),
                turn=int(data["turn"]),
                last_turn=data["last_turn"],
            )
        except (KeyError, TypeError, ValueError):
            return None


def _head(path: Path, size: int) -> str:
    with path.open("rb") as handle:
        return hashlib.sha256(handle.read(min(size, _HEAD_BYTES))).hexdigest()


def _cached(cache: Path | None, path: Path, since: str | None) -> _State:
    """The cached scan of this transcript, if it still describes the file's
    start; otherwise a fresh state that reads it from the beginning."""
    fresh = _State(since=since)
    if cache is None:
        return fresh
    try:
        state = _State.from_json(json.loads(cache.read_text(encoding="utf-8")))
        info = os.stat(path)
    except (OSError, ValueError):
        return fresh
    if state is None or state.since != since or state.inode != info.st_ino:
        return fresh
    if info.st_size < state.offset or _head(path, state.offset) != state.head:
        return fresh
    return state


def _save(cache: Path | None, state: _State) -> None:
    if cache is None:
        return
    tmp = cache.with_name(cache.name + ".tmp")
    try:
        tmp.write_text(json.dumps(state.to_json()), encoding="utf-8")
        os.replace(tmp, cache)
    except OSError:
        pass


def _read(entry: dict, at: int, state: _State, floor: datetime | None) -> None:
    """Fold one decoded transcript entry into the scan's state."""
    if not isinstance(entry, dict) or entry.get("isSidechain") is True:
        return
    found = _turn_of(entry, at)
    if found is not None and found != state.last_turn:
        state.turn += 1
        state.last_turn = found
    state.commands.update(_background_commands(entry))
    result = entry.get("toolUseResult")
    if isinstance(result, dict):
        launch = _launch_id(result)
        if launch is not None and not _before(entry, floor):
            task, any_turn = launch
            state.launched[task] = None if any_turn else state.turn
            state.finished.discard(task)
            command = state.commands.pop(_result_tool_use_id(entry) or "", None)
            if not any_turn and command is not None and guard.ci_watch(command):
                state.watches.add(task)
        stopped = _stopped_id(result)
        if stopped is not None:
            state.finished.add(stopped)
        return
    state.finished.update(_delivered_ids(entry))


def scan(
    path: Path,
    since: str | None = None,
    budget_s: float = SCAN_BUDGET_S,
    cache: Path | None = None,
) -> Pending:
    """The background tasks launched at or after ``since`` that have neither
    reported nor been stopped: every agent and Monitor, and the shell commands
    of the last turn. ``shell`` leaves out CI watches.

    With ``cache``, the scan goes on from the state it saved there last time.
    Raises ``OSError`` when the file cannot be read, and ``ScanTooSlow`` past
    ``budget_s``: the caller logs either and treats it as no task, so a turn is
    judged as it always was rather than the hook timing out with no verdict.
    """
    floor = ps.parse_iso(since) if since else None
    state = _cached(cache, path, since)
    deadline = time.monotonic() + budget_s
    unclocked = 0
    with path.open("rb") as handle:
        state.inode = os.fstat(handle.fileno()).st_ino
        handle.seek(state.offset)
        at = state.offset
        for line in handle:
            if not line.endswith(b"\n"):
                # A line still being written: read it whole next time.
                break
            start, at = at, at + len(line)
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
            _read(entry, start, state, floor)
    state.offset = at
    state.head = _head(path, at)
    _save(cache, state)
    left = [
        (t, turn)
        for t, turn in state.launched.items()
        if t not in state.finished and turn in (None, state.turn)
    ]
    return Pending(
        tuple(t for t, _ in left),
        tuple(t for t, turn in left if turn is not None and t not in state.watches),
    )
