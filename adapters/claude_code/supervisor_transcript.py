#!/usr/bin/env python3
"""Read the pending gate from one session transcript.

The autoplan approval question and the ``/review`` question batch are
``AskUserQuestion`` tool calls. A Claude Code permission prompt ("Do you want to
proceed?") is a different call: a ``Bash`` (or other) ``tool_use`` block with no
matching ``tool_result``. The Stop hook never fires while either is pending, so
the state file shows ``running`` with a stale ``updated_at`` and nothing else;
the gate itself lives only in the session transcript. Given ``--session-id``,
this tool finds the transcript and returns the last pending gate.

The result is one of five outcomes, never collapsed to "nothing": ``pending`` (an
``AskUserQuestion``, with its ``tool_use_id``), ``permission_prompt`` (a last
unresolved non-question ``tool_use`` older than 60 seconds, with its ``tool``,
``command`` and ``tool_use_id``), ``none`` (no pending gate), ``missing`` (no
transcript for that session), and ``unreadable`` (a permission error, a parse
error, a truncated live line, or an ambiguous duplicate session match).

A confirmed ``AskUserQuestion`` outranks a permission prompt: a session blocked on
a prompt cannot emit a later ``tool_use``, so an unresolved question anywhere is
the real gate. A tool call younger than 60 seconds, or on a line without a
timestamp, reads as ``none``: it is a tool in flight whose result is about to
arrive, and escalating it would fire a false alarm every scan.

The tool never writes and never touches the network.
"""

from __future__ import annotations

import argparse
import glob as globlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from core import pipeline_state as ps

# An adapter, not core: this module knows one harness's transcript layout
# and its AskUserQuestion blocks, so it cannot live behind the neutral seam.
DEFAULT_ROOT = "~/.claude/projects"
_ASK = "AskUserQuestion"
_PROMPT_AGE_S = 60
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def valid_session_id(session_id: str) -> bool:
    """A session id names one file, never a path or a wildcard."""
    return bool(_ID_RE.match(session_id)) and session_id not in (".", "..")


def find_transcript(root: Path, session_id: str) -> Path | str | None:
    """The transcript path, ``None`` if missing, or ``"ambiguous"`` if many.

    The recursive search is authoritative for ambiguity, so a shallow match is
    never returned when a second copy sits deeper under the root.
    """
    hits = globlib.glob(str(root / "**" / f"{session_id}.jsonl"), recursive=True)
    matches = sorted({Path(p).resolve() for p in hits if Path(p).is_file()})
    if not matches:
        return None
    return "ambiguous" if len(matches) > 1 else matches[0]


def _blocks(line: object) -> list:
    if not isinstance(line, dict):
        return []
    message = line.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        content = line.get("content")
    return content if isinstance(content, list) else []


def _absorb(
    block: object, timestamp: object, sidechain: bool, tool_uses: list, resolved: set
) -> None:
    """Sort one content block into the tool_use list or the resolved id set."""
    if not isinstance(block, dict):
        return
    kind = block.get("type")
    if kind == "tool_use":
        # A tool call with no string id cannot be correlated with a result, and
        # an unhashable id (a list or dict) would crash the pending set test, so
        # a malformed call is dropped rather than tracked.
        if isinstance(block.get("id"), str):
            tool_uses.append((block, timestamp, sidechain))
    elif kind == "tool_result":
        rid = block.get("tool_use_id")
        if isinstance(rid, str):
            resolved.add(rid)


def _collect(lines: list) -> tuple[list[tuple[dict, object, bool]], set]:
    """Return every tool_use block, with its line timestamp and sidechain flag.

    The second member is the set of resolved ``tool_use_id``s. Every real
    transcript line carries a top-level ``timestamp`` and may carry
    ``isSidechain``; both are needed to age a pending call and to exclude a
    subagent's own in-flight call.
    """
    tool_uses: list[tuple[dict, object, bool]] = []
    resolved: set = set()
    for line in lines:
        timestamp = line.get("timestamp") if isinstance(line, dict) else None
        sidechain = bool(line.get("isSidechain")) if isinstance(line, dict) else False
        for block in _blocks(line):
            _absorb(block, timestamp, sidechain, tool_uses, resolved)
    return tool_uses, resolved


def _command(block: dict) -> str:
    """The ``command`` field of a tool call, or an empty string."""
    data = block.get("input")
    command = data.get("command") if isinstance(data, dict) else None
    return command if isinstance(command, str) else ""


def _older_than(timestamp: object, now: datetime, seconds: int) -> bool:
    """True when ``timestamp`` parses and is more than ``seconds`` before ``now``."""
    parsed = ps.parse_iso(timestamp)
    if parsed is None:
        return False
    return (now - parsed).total_seconds() > seconds


def _options(question: dict) -> list[dict]:
    out = []
    for opt in question.get("options", []):
        label = opt.get("label") if isinstance(opt, dict) else opt
        label = label if isinstance(label, str) else ""
        out.append({"label": label, "recommended": "(recommended)" in label.lower()})
    return out


def _questions(block: dict) -> list:
    data = block.get("input")
    questions = data.get("questions") if isinstance(data, dict) else None
    return questions if isinstance(questions, list) else []


def _shape(block: dict) -> dict:
    questions = _questions(block)
    prompt = "\n".join(
        q.get("question", "")
        for q in questions
        if isinstance(q, dict) and isinstance(q.get("question"), str)
    )
    options: list[dict] = []
    for q in questions:
        if isinstance(q, dict):
            options.extend(_options(q))
    return {"tool_use_id": block.get("id"), "prompt": prompt, "options": options}


def _load_lines(path: Path) -> list[dict] | None:
    """Every JSON line, or ``None`` when any line fails to parse."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    lines = []
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            lines.append(json.loads(raw))
        except (json.JSONDecodeError, ValueError):
            return None
    return lines


def _resolve_lines(root: Path, session_id: str) -> list | str:
    """The parsed transcript lines, or an outcome string when they cannot load."""
    if not valid_session_id(session_id):
        return "unreadable"
    found = find_transcript(root, session_id)
    if found is None:
        return "missing"
    if found == "ambiguous":
        return "unreadable"
    lines = _load_lines(found)
    return "unreadable" if lines is None else lines


def _permission_prompt(block: dict) -> dict:
    return {
        "outcome": "permission_prompt",
        "tool": block.get("name"),
        "command": _command(block),
        "tool_use_id": block.get("id"),
    }


def _pending_gate(lines: list, now: datetime) -> dict:
    """The gate outcome (without ``session_id``) for one loaded transcript."""
    tool_uses, resolved = _collect(lines)
    pending = [t for t in tool_uses if t[0].get("id") not in resolved]
    # A confirmed question outranks a speculative permission prompt.
    asks = [block for block, _ts, _side in pending if block.get("name") == _ASK]
    if asks:
        return {"outcome": "pending", **_shape(asks[-1])}
    # The last unresolved non-sidechain tool call is the one that blocks.
    blockers = [(block, ts) for block, ts, side in pending if not side]
    if not blockers:
        return {"outcome": "none"}
    block, timestamp = blockers[-1]
    if not _older_than(timestamp, now, _PROMPT_AGE_S):
        return {"outcome": "none"}
    return _permission_prompt(block)


def read_question(root: Path, session_id: str, now: datetime | None = None) -> dict:
    """The pending-gate outcome for one session.

    ``now`` overrides the clock the 60-second prompt age is measured against; it
    defaults to the current UTC time.
    """
    if now is None:
        now = datetime.now(UTC)
    lines = _resolve_lines(root, session_id)
    if isinstance(lines, str):
        return {"outcome": lines, "session_id": session_id}
    return {"session_id": session_id, **_pending_gate(lines, now)}


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read a session's pending gate.")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcripts-dir", default=DEFAULT_ROOT)
    parser.add_argument("--now", default=None, help="ISO-8601 override for tests")
    return parser.parse_args(argv)


def _now_from(arg: str | None) -> datetime:
    if arg is None:
        return datetime.now(UTC)
    parsed = ps.parse_iso(arg)
    if parsed is None:
        raise SystemExit("--now must be an ISO-8601 timestamp with an offset")
    return parsed


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.transcripts_dir).expanduser()
    result = read_question(root, args.session_id, _now_from(args.now))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
