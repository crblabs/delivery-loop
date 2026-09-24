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

# An adapter, not core: this module knows one harness's transcript layout,
# its AskUserQuestion blocks, its hook attachments and its permission modes.
DEFAULT_ROOT = "~/.claude/projects"
_ASK = "AskUserQuestion"
_PROMPT_AGE_S = 60
_BYPASS = "bypassPermissions"
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
    block: object,
    timestamp: object,
    sidechain: bool,
    mode: object,
    tool_uses: list,
    resolved: set,
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
            tool_uses.append((block, timestamp, sidechain, mode))
    elif kind == "tool_result":
        rid = block.get("tool_use_id")
        if isinstance(rid, str):
            resolved.add(rid)


def _stdout_allows(stdout: object) -> bool:
    if not isinstance(stdout, str) or not stdout.strip():
        return False
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return False
    hso = data.get("hookSpecificOutput") if isinstance(data, dict) else None
    return isinstance(hso, dict) and hso.get("permissionDecision") == "allow"


def _absorb_allow(line: dict, allowed: set) -> None:
    # A PreToolUse allow is its own top-level attachment line, read here not via
    # _blocks. Only a succeeded hook was honored; a malformed one adds no id.
    att = line.get("attachment")
    if not isinstance(att, dict) or att.get("type") != "hook_success":
        return
    if att.get("hookEvent") != "PreToolUse":
        return
    tid = att.get("toolUseID")
    if isinstance(tid, str) and _stdout_allows(att.get("stdout")):
        allowed.add(tid)


def _collect(lines: list) -> tuple[list[tuple[dict, object, bool, object]], set, set]:
    """Every tool_use (timestamp, sidechain, mode), the resolved ids, and the ids
    a ``PreToolUse`` hook answered ``allow``.
    """
    tool_uses: list[tuple[dict, object, bool, object]] = []
    resolved: set = set()
    allowed: set = set()
    mode: object = None
    for line in lines:
        if not isinstance(line, dict):
            continue
        timestamp = line.get("timestamp")
        sidechain = bool(line.get("isSidechain"))
        _absorb_allow(line, allowed)
        pm = line.get("permissionMode")
        # Only a main-session line updates the mode, so a subagent cannot mask a
        # call; each call keeps the mode in force at it, never a later one.
        if isinstance(pm, str) and not sidechain:
            mode = pm
        for block in _blocks(line):
            _absorb(block, timestamp, sidechain, mode, tool_uses, resolved)
    return tool_uses, resolved, allowed


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
    # splitlines also breaks on U+0085 and other Unicode line boundaries, which a
    # JSON string value may hold; JSONL delimits records on "\n" only.
    for raw in text.split("\n"):
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


def _blocking_prompt(pending: list, allowed: set, now: datetime) -> dict | None:
    # A call the guard allowed, or one under bypass mode, is not waiting on a
    # human, so it is filtered out per candidate: one message can carry several
    # calls, so an allowed later one must not hide an earlier genuine one.
    blockers = [
        (block, ts)
        for block, ts, side, mode in pending
        if not side and block.get("id") not in allowed and mode != _BYPASS
    ]
    if not blockers:
        return None
    block, timestamp = blockers[-1]
    return block if _older_than(timestamp, now, _PROMPT_AGE_S) else None


def _pending_gate(lines: list, now: datetime) -> dict:
    """The gate outcome (without ``session_id``) for one loaded transcript."""
    tool_uses, resolved, allowed = _collect(lines)
    pending = [t for t in tool_uses if t[0].get("id") not in resolved]
    # A question outranks a prompt, even under bypass: bypass never answers a
    # question, and catching it unattended is the whole point.
    asks = [block for block, _ts, _side, _mode in pending if block.get("name") == _ASK]
    if asks:
        return {"outcome": "pending", **_shape(asks[-1])}
    block = _blocking_prompt(pending, allowed, now)
    return _permission_prompt(block) if block else {"outcome": "none"}


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
