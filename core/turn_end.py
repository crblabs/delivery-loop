"""What the loop does when the agent ends a turn: advance, pause, finish or push on.

``handle_turn_end`` is capability 1 of ``docs/adapter-contract.md``. The adapter
parses the harness event into a ``TurnEnd`` and returns this verdict to the
harness. The verdict either lets the turn end or blocks it and reinjects text
the agent reads next.

A stage ends on a token in the last line of the agent's message:

  * ``<promise>STAGE DONE</promise>``: the stage is done. Its ``emits`` value
    (``PLAN: <path>``, ``PR: <url>``) must come before it, and a ``clean_tree``
    stage needs a committed worktree. The run moves to the next stage, or ends.
    A stage with a gate stops for a person after it.
  * ``<promise>NEEDS HUMAN</promise>``: the stage stops for a person. The
    decision card above it is kept as the pending question.

Any other turn end is blocked and the stage reinjected, counted in the stage's
``attempts``. At ``caps.attempts`` the run pauses with ``no_message``, so a run
that keeps missing the token reaches a person without one typing in between.

Every turn end also hashes the guard map: the carve-outs, the ``loop_exact``
files, every file under ``loop_prefixes``, the ``guard_watch`` files and the
run's config snapshot. The two settings files are hashed by the keys that can
switch the hooks off, because the harness rewrites the rest of a settings file
whenever a person answers a permission prompt. A change the approved plan
declared becomes the new baseline; any other change pauses the run with
``guard_changed``, in the shape ``supervisor_decide.changed_guard_paths`` reads.
That catches a shell edit, which the edit guard never sees.

    turn end ── another session? ─────────────────────────────> let it end
        │ guard map changed off the declared edits? ─────────> pause guard_changed
        │ NEEDS HUMAN ────────────────────────────────────────> pause needs_human
        │ STAGE DONE ── emits missing / tree dirty ─> block, reinject the stage
        │            └─ last stage ─> done   gate ─> pause gate   else ─> block, next stage
        └ no token ── attempts < cap ─> block, reinject   at cap ─> pause no_message
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from core import run_index as ri
from core import run_state as rs
from core.config import LoopConfig
from core.guard_evidence import declared, guard_map
from core.pipeline_loop_paths import parse_loop_edits_block

_TAIL_CHARS = 500


@dataclass(frozen=True)
class TurnEnd:
    """One turn end of a session: who ended it, and the message it ended on."""

    session_id: str | None
    message: str | None
    prompt_id: str | None = None


@dataclass(frozen=True)
class TurnEndVerdict:
    block: bool
    reinject: str | None = None
    pause_reason: str | None = None


PASS = TurnEndVerdict(block=False)


def _last_line(message: str) -> str:
    for line in reversed(message.splitlines()):
        stripped = line.strip().strip("*_").strip()
        if stripped:
            return stripped
    return ""


def _emitted(message: str, emits: str) -> str | None:
    """The value a stage's ``emits`` names, such as the path after ``PLAN:``."""
    label = emits.split(":", 1)[0].strip()
    if not label or ":" not in emits:
        return ""
    for line in reversed(message.splitlines()):
        stripped = line.strip().strip("`*_ ")
        if stripped.startswith(label + ":"):
            value = stripped[len(label) + 1 :].strip().strip("`")
            if value:
                return value
    return None


def _dirty(worktree: Path) -> bool | None:
    try:
        done = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return None if done.returncode != 0 else bool(done.stdout.strip())


# A plan is prose; anything larger is not one.
MAX_PLAN_BYTES = 1 << 20
_URL_RE = re.compile(r"^https?://\S+$")


def _plan_path(plan: str, worktree: Path) -> Path:
    path = Path(plan).expanduser()
    return path if path.is_absolute() else worktree / path


def _read_plan(path: Path) -> str | None:
    """The plan's text, from a small regular file only, never waiting on a pipe.

    The path comes from the agent's message, and a Stop hook that runs past its
    timeout lets the turn end, so a device, a FIFO or a huge file is refused.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_PLAN_BYTES:
            return None
        data = handle.read(MAX_PLAN_BYTES + 1)
    return data.decode("utf-8", errors="replace")


def _block(state: dict, text: str) -> TurnEndVerdict:
    # Recorded for the event trail: the revision this block wrote. Reentrant
    # blocks are bounded by the stage's attempts, not by this field.
    state["last_block_revision"] = state["revision"] + 1
    return TurnEndVerdict(block=True, reinject=text)


def _no_token(run: rs.Run, state: dict, why: str) -> TurnEndVerdict:
    stage = state["current_stage"]
    state["attempts"][stage] += 1
    state["total_attempts"] += 1
    cap = (state.get("caps") or {}).get("attempts", rs.MAX_ATTEMPTS)
    if state["attempts"][stage] >= cap:
        rs.pause(
            state,
            "no_message",
            f"Stage {stage} ended {state['attempts'][stage]} turns in a row without a token "
            f"({why}). Read the transcript, then {run.config.resume_command} or "
            f"{run.config.abort_command}.",
        )
        return TurnEndVerdict(block=False, pause_reason="no_message")
    return _block(state, f"The stage is not finished: {why}.\n\n{rs.state_text(run, state)}")


def _gate_card(name: str, gate: str, state: dict, config: LoopConfig) -> str:
    """The card for a gate: what is being approved, the plan file the loop read and
    the loop files it unlocks, so a person approves what the run will act on."""
    plan = state.get("plan_path")
    edits = state.get("declared_loop_edits") or []
    lines = []
    if plan:
        lines.append(f"Plan read: {plan}")
        lines.append("Loop files it unlocks: " + (", ".join(edits) if edits else "none"))
    lines += [
        f"ASK   Stage {name} is done and asks for a person ({gate}). Continue to "
        f"{state['current_stage']}?",
        f"REC   1. {config.resume_command}",
        f"ALT   2. {config.abort_command}",
        "REPLY 1 | 2",
    ]
    return "\n".join(lines)


def _advance(run: rs.Run, state: dict, message: str) -> TurnEndVerdict:
    config = run.config
    stage = config.stages[state["current"]]
    if stage.emits:
        value = _emitted(message, stage.emits)
        if value is None:
            return _no_token(run, state, f"the stage must write {stage.emits} before the token")
        label = stage.emits.split(":", 1)[0].strip().upper()
        if label == "PLAN" and value:
            path = _plan_path(value, run.worktree)
            text = _read_plan(path)
            if text is None:
                return _no_token(
                    run, state, f"PLAN: {value} is not a readable plan file of at most 1 MB"
                )
            state["plan_path"] = str(path)
            state["declared_loop_edits"] = parse_loop_edits_block(text, config)
        elif label == "PR" and value:
            if not _URL_RE.match(value):
                return _no_token(run, state, f"PR: {value} is not the pull request's url")
            state["pr_url"] = value
    if stage.clean_tree and _dirty(run.worktree) is not False:
        return _no_token(run, state, "the worktree has uncommitted changes; commit them first")
    state["history"].append({"at": ri.now_iso(), "event": "done", "stage": stage.name})
    if state["current"] + 1 >= len(config.stages):
        state["status"] = "done"
        state["ended_reason"] = "done"
        return PASS
    state["current"] += 1
    state["current_stage"] = config.stages[state["current"]].name
    state["attempts"][state["current_stage"]] = 0
    if stage.gate != "none":
        rs.pause(state, "gate", _gate_card(stage.name, stage.gate, state, config))
        return TurnEndVerdict(block=False, pause_reason="gate")
    return _block(state, rs.state_text(run, state))


def _check_guard(run: rs.Run, state: dict) -> TurnEndVerdict | None:
    current = guard_map(run.worktree, run.config, run.run_dir)
    baseline = state.get("guard_baseline")
    if not isinstance(baseline, dict) or not baseline:
        state["guard_baseline"] = current
        state["guard_files_seen"].append(current)
        return None
    changed = sorted(k for k in set(baseline) | set(current) if baseline.get(k) != current.get(k))
    edits = state.get("declared_loop_edits") or []
    accepted = [k for k in changed if declared(k, edits)]
    if accepted:
        for k in accepted:
            if k in current:
                baseline[k] = current[k]
            else:
                baseline.pop(k, None)
        changed = [k for k in changed if k not in accepted]
    if not changed:
        return None
    state["guard_pending"] = current
    rs.pause(
        state,
        "guard_changed",
        f"Loop guard files changed ({', '.join(changed)}); paused. A person checks them, then "
        f"{run.config.resume_command} accepts them or {run.config.abort_command} ends the run.",
    )
    return TurnEndVerdict(block=False, pause_reason="guard_changed")


def handle_turn_end(event: TurnEnd, run: rs.Run, timeout: float = ri.HOOK_LOCK_S) -> TurnEndVerdict:
    """Decide one turn end of an active run, writing the state once under its lock."""

    def apply(state: dict) -> TurnEndVerdict:
        if state["status"] != "running":
            return PASS
        if state.get("session_id") is None and event.session_id:
            state["session_id"] = event.session_id
        elif state.get("session_id") != event.session_id:
            return PASS
        state["hook_seen"] = ri.now_iso()
        verdict = _check_guard(run, state)
        if verdict is not None:
            return verdict
        message = event.message or ""
        last = _last_line(message)
        if last == rs.PAUSE_TOKEN:
            rs.pause(state, "needs_human", message[-_TAIL_CHARS:], event.prompt_id)
            return TurnEndVerdict(block=False, pause_reason="needs_human")
        if last == rs.DONE_TOKEN:
            return _advance(run, state, message)
        return _no_token(run, state, "the last line was not a token")

    _, state = rs.read(run)
    if state is None or state.get("status") != "running":
        return PASS
    bound = state.get("session_id")
    if bound is not None and bound != event.session_id:
        return PASS
    return rs.update(run, apply, timeout)
