"""Claude Code's hooks, mapped onto ``core.turn_end``, ``core.guard`` and ``core.run_state``.

The plugin's ``hooks/hooks.json`` runs three shims: ``pipeline_stop.py`` on
``Stop``, ``pipeline_guard.py`` on ``PreToolUse`` and ``pipeline_prompt.py`` on
``UserPromptSubmit``. Each checks the interpreter, then calls ``run_stop``,
``run_guard`` or ``run_prompt`` here with the raw payload and prints the result
with ``emit``. This module and ``bootstrap`` together are the only place that
knows the payload fields and the output shapes:

  * ``Stop``: ``session_id``, ``cwd``, ``stop_hook_active`` (the turn end follows
    a block of ours), ``last_assistant_message`` and ``transcript_path``. To
    block, print ``{"decision": "block", "reason": <text the agent reads>}``.
  * ``PreToolUse``: ``session_id``, ``cwd``, ``tool_name`` and ``tool_input``. To
    deny, print a ``hookSpecificOutput`` with ``permissionDecision: "deny"``. To
    allow, print nothing, so the user's own permission rules still apply.
  * ``UserPromptSubmit``: ``session_id``, ``cwd`` and ``prompt``. A typed resume
    or abort is applied here, and its result is ``additionalContext``.

The error path the adapter contract left open is settled here. A payload that
does not parse fails open: there is no run to protect. Any error once a run is
found fails closed: the guard denies, and the Stop hook blocks once per turn
(never when ``stop_hook_active`` is set, so it cannot loop). A run whose index
entry names no state file any more is stale: the entry is removed and the call
passes.

Every decision on a run is one line in the run's ``events.jsonl``. The core
modules are imported where they are used, so a call loads only what it needs.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from adapters.claude_code import bootstrap as boot
from core import run_index as ri

TAIL_BYTES = 256 * 1024
_TRANSCRIPT_TRIES = 3
_TRANSCRIPT_WAIT_S = 0.05
# The Stop hook may wait this long for a person's command to finish writing.
STOP_LOCK_S = 5.0


@dataclass(frozen=True)
class HookResult:
    stdout: str = ""
    stderr: str = ""


PASS = HookResult()


def emit(result: HookResult) -> int:
    """Print a result the way the harness reads it; a hook always exits 0."""
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        sys.stderr.write(result.stderr + "\n")
    return 0


def block(reason: str) -> HookResult:
    return HookResult(stdout=boot.block_json(reason))


def deny(reason: str) -> HookResult:
    return HookResult(stdout=boot.deny_json(reason))


def _abort_hint(abort_command: str = f"/{boot.COMMAND_NAME} abort") -> str:
    return (
        f"Fix: delivery-loop status to see the run; a person types {abort_command} to end "
        f"it. Details in the run's events.jsonl. See {ri.README_URL}runbook"
    )


def _text_of(entry: dict) -> str | None:
    message = entry.get("message")
    if entry.get("type") != "assistant" or not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    texts = [t for t in texts if isinstance(t, str)]
    return "\n".join(texts) if texts else None


def transcript_tail_message(path: Path) -> str | None:
    """The assistant text that ended the current turn, from the transcript's tail.

    Only entries after the last user entry (a prompt or a tool result) count: a
    turn that ended on a tool call has no closing text, and an earlier turn's
    token must not be read as this one's.
    """
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(data.splitlines()):
        try:
            entry = json.loads(line)
        except (ValueError, RecursionError):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "user":
            return None
        text = _text_of(entry)
        if text is not None:
            return text
    return None


def last_message(payload: dict) -> str | None:
    """The message the turn ended on: the payload's own field, else the transcript's tail.

    The transcript can lag the Stop event, so it is read up to three times.
    """
    message = payload.get("last_assistant_message")
    if isinstance(message, str):
        return message
    path = payload.get("transcript_path")
    if not isinstance(path, str):
        return None
    for attempt in range(_TRANSCRIPT_TRIES):
        text = transcript_tail_message(Path(path))
        if text is not None and text.strip():
            return text
        if attempt + 1 < _TRANSCRIPT_TRIES:
            time.sleep(_TRANSCRIPT_WAIT_S)
    return None


def _closed(payload: dict, reason: str) -> HookResult:
    """Fail closed: deny the tool, or block the turn once."""
    if payload.get("hook_event_name") == "PreToolUse":
        return deny(reason)
    if payload.get("stop_hook_active") is True:
        return HookResult(stderr=reason)
    return block(reason)


def _open(payload: dict, key: str, entry: dict):
    """(run, None), or (None, the result when it cannot be judged)."""
    from core import run_state as rs

    state_path = Path(ri.state_file(entry["run_dir"]))
    if not state_path.exists():
        ri.remove_entry(key, entry.get("session_id"))
        return None, HookResult(
            stderr=f"delivery-loop: removed a stale index entry for {entry.get('worktree')} "
            "(its run directory has no state file)."
        )
    run = rs.open_run(key, entry)
    condition, state = rs.read(run)
    if condition == "unsupported":
        return None, _closed(
            payload,
            "This run was started by another delivery-loop version. Fix: finish it with that "
            f"version, or a person types {run.config.abort_command}.",
        )
    if state is None:
        reason = f"delivery-loop: the run's state is {condition}. "
        return None, _closed(payload, reason + _abort_hint(run.config.abort_command))
    return run, None


def _pause_note(run, reason: str) -> str:
    from core import run_state as rs

    state = rs.read(run)[1] or {}
    card = (state.get("pending_question") or "").strip()
    head = f"delivery-loop paused this run ({reason})."
    tail = f"Continue: {run.config.resume_command}. End: {run.config.abort_command}."
    return "\n".join(part for part in (head, card if reason != "needs_human" else "", tail) if part)


def _session(payload: dict) -> str | None:
    session = payload.get("session_id")
    return session if isinstance(session, str) else None


def run_stop(raw: str) -> HookResult:
    payload = boot.payload_of(raw)
    if payload is None:
        # No cwd to find a run with, so nothing to fail closed for.
        return HookResult(stderr="delivery-loop: the Stop payload did not parse; ignored.")
    payload.setdefault("hook_event_name", "Stop")
    found = ri.find_entry(payload.get("cwd"), None, payload.get("session_id"))
    if found is None:
        return PASS
    run_dir = found[1]["run_dir"]
    try:
        from core import turn_end as te

        run, early = _open(payload, *found)
        if run is None:
            return early or PASS
        prompt_id = payload.get("prompt_id")
        event = te.TurnEnd(
            session_id=_session(payload),
            message=last_message(payload),
            prompt_id=prompt_id if isinstance(prompt_id, str) else None,
        )
        verdict = te.handle_turn_end(event, run, STOP_LOCK_S)
    except Exception as exc:  # noqa: BLE001 - any failure on a run fails closed, once
        ri.append_event(run_dir, {"hook": "stop", "decision": "error", "reason": repr(exc)})
        return _closed(
            payload, f"delivery-loop could not record this turn: {exc!r}. " + _abort_hint()
        )
    ri.append_event(
        run_dir,
        {
            "hook": "stop",
            "session": event.session_id,
            "decision": "block" if verdict.block else ("pause" if verdict.pause_reason else "pass"),
            "reason": verdict.pause_reason,
        },
    )
    if verdict.block and verdict.reinject:
        return block(verdict.reinject)
    if verdict.pause_reason:
        # The turn ends here; show the person why, in the session they are in.
        return HookResult(stdout=boot.system_message_json(_pause_note(run, verdict.pause_reason)))
    return PASS


def run_guard(raw: str) -> HookResult:
    """The guard, failing closed on any error once the call touches a run."""
    payload = boot.payload_of(raw)
    if payload is None:
        return HookResult(stderr="delivery-loop: the PreToolUse payload did not parse; allowed.")
    payload.setdefault("hook_event_name", "PreToolUse")
    try:
        return _guard(payload)
    except Exception as exc:  # noqa: BLE001 - a guard that crashes must not let the call run
        try:
            touched = bool(
                ri.find_entries(payload.get("cwd"), boot.target_of(payload), _session(payload))
            )
        except Exception:  # noqa: BLE001
            touched = True
        if touched:
            return deny(f"delivery-loop could not check this call: {exc!r}. " + _abort_hint())
        return HookResult(stderr=f"delivery-loop: the guard failed with no run here: {exc!r}")


def _guard(payload: dict) -> HookResult:
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    sub = boot.human_only_call(tool_name, tool_input)
    if sub is not None:
        return deny(boot.human_only_message(sub))
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    session = _session(payload)
    if tool_name == "Bash" and ri.starts_run(command) and session is not None:
        root = ri.worktree_root(str(payload.get("cwd") or "."))
        if root is not None:
            # Best effort: a start without its intent binds on the first turn end.
            with contextlib.suppress(OSError):
                ri.write_intent(ri.worktree_key(root), session)
    target = boot.target_of(payload)
    found = ri.find_entries(payload.get("cwd"), target, payload.get("session_id"))
    if not found:
        return PASS
    stderr = []
    for key, entry in found:
        try:
            from core import guard

            run, early = _open(payload, key, entry)
            if run is None:
                if early is not None and early.stdout:
                    return early
                stderr += [early.stderr] if early is not None and early.stderr else []
                continue
            call = guard.PreToolCall(
                session_id=session,
                kind=boot.kind_of(tool_name),
                tool_name=str(tool_name),
                cwd=payload.get("cwd") if isinstance(payload.get("cwd"), str) else None,
                command=command if isinstance(command, str) else None,
                path=target if isinstance(target, str) else None,
            )
            verdict = guard.handle_pre_tool(call, run)
        except Exception as exc:  # noqa: BLE001 - any failure on a run fails closed
            ri.append_event(
                entry["run_dir"], {"hook": "guard", "decision": "error", "reason": repr(exc)}
            )
            return deny(f"delivery-loop could not check this call: {exc!r}. " + _abort_hint())
        if not verdict.allow:
            ri.append_event(
                str(run.run_dir),
                {"hook": "guard", "session": session, "decision": "deny", "reason": verdict.reason},
            )
            return deny(verdict.reason or "denied by delivery-loop")
    return HookResult(stderr="\n".join(stderr))


def run_prompt(raw: str) -> HookResult:
    """Apply a person's typed resume or abort, and tell the agent what happened."""
    payload = boot.payload_of(raw)
    sub = boot.prompt_command(payload.get("prompt")) if payload is not None else None
    if payload is None or sub is None:
        return PASS
    found = ri.find_entry(payload.get("cwd"), None, payload.get("session_id"))
    if found is None:
        return HookResult(stdout=boot.context_json("No active delivery-loop run in this worktree."))
    try:
        if sub == "abort":
            text = ri.abort_run(found[0], found[1], "person")
        else:
            from core import run_state as rs

            text = rs.resume(rs.open_run(*found), "person", _session(payload))
    except Exception as exc:  # noqa: BLE001 - report any failure to the person, change nothing
        text = f"delivery-loop could not {sub} the run: {exc}"
    return HookResult(stdout=boot.context_json(f"delivery-loop {sub}: " + text))
