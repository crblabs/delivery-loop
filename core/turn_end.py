"""What the loop does when the agent ends a turn: advance, pause, finish or push on.

``handle_turn_end`` is capability 1 of ``docs/adapter-contract.md``. The adapter
parses the harness event into a ``TurnEnd`` and returns this verdict to the
harness. The verdict either lets the turn end or blocks it and reinjects text
the agent reads next.

A stage ends on a token in the last line of the agent's message:

  * ``<promise>STAGE DONE</promise>``: the stage is done. Its ``emits`` value
    (``PLAN: <path>``, ``PR: <url>``) must come before it, and a ``clean_tree``
    stage needs a committed worktree that passes the no-unicode-dash rule. The
    run moves to the next stage, or ends. A stage with a gate stops for a person
    after it.
  * ``<promise>NEEDS HUMAN</promise>``: the stage stops for a person. The
    decision card above it is kept as the pending question.

Any other turn end is blocked and the stage reinjected, counted in the stage's
``attempts``. At ``caps.attempts`` the run pauses with ``no_message``, so a run
that keeps missing the token reaches a person without one typing in between.

A turn that ends while a background task the agent started still runs is
different: the agent is waiting for the task's notification, which wakes the
session. The adapter reports those tasks in ``TurnEnd.pending_tasks``. A turn
end with no token is then let through without spending an attempt, up to
``caps.waits`` times in one wait, after which the run pauses for a person with
the tasks named (``wait_capped``). A turn end with nothing pending ends the wait
and restarts that count. ``STAGE DONE`` is refused while a task runs, so the
next stage never starts beside a reviewer that is still working; that refusal
spends an attempt. A pause on such refusals marks the tasks stuck only the
second time in a row beside them, once they have waited ``STUCK_AFTER_S``: a
lost notification is the only way they get there, never a quick agent. Every turn end records the
tasks in ``waiting_on`` and when the wait began in ``waiting_since``; a person's
resume can release tasks that will never report.

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
        │ background task running ── STAGE DONE ─> block, reinject (an attempt)
        │                          └ no token ── waits < cap ─> let it end (a wait)
        │                                       at cap ─> pause no_message, wait_capped
        │                            at cap ─> pause no_message; again beside the
        │                                      same tasks, waited 30 min ─> stuck
        │ tasks unreadable ── STAGE DONE ─> block, reinject (an attempt)
        │ STAGE DONE ── emits missing / tree dirty / dash ─> block, reinject
        │            └─ last stage ─> done   gate ─> pause gate   else ─> block, next stage
        └ no token ── attempts < cap ─> block, reinject   at cap ─> pause no_message
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core import no_unicode_dash as nd
from core import pipeline_state as ps
from core import run_index as ri
from core import run_state as rs
from core.config import LoopConfig, git_env
from core.guard_evidence import declared, guard_map
from core.pipeline_loop_paths import is_carveout, parse_loop_edits_block

_TAIL_CHARS = 500


@dataclass(frozen=True)
class TurnEnd:
    """One turn end of a session: who ended it, and the message it ended on."""

    session_id: str | None
    message: str | None
    prompt_id: str | None = None
    # The background tasks the agent started that have not reported yet, as
    # the adapter reads them; empty when the harness cannot say.
    pending_tasks: tuple[str, ...] = ()
    # Which of them are shell commands, as opposed to agents, which always report.
    shell_tasks: tuple[str, ...] = ()
    # True when the adapter could not read the tasks: STAGE DONE is refused, and
    # the wait the run records is left as it was.
    tasks_unknown: bool = False
    # A file the harness writes while the session works, for the supervisor.
    activity_path: str | None = None


@dataclass(frozen=True)
class TurnEndVerdict:
    block: bool
    reinject: str | None = None
    pause_reason: str | None = None
    # Shown to the person without blocking, such as how to adopt a run.
    note: str | None = None
    # The tasks the turn ends waiting on, after a person's releases; their
    # notification wakes the session.
    tasks: tuple[str, ...] = ()

    @property
    def waiting(self) -> bool:
        return bool(self.tasks)


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


# ``git status`` walks the whole tree, so it gets longer than a one-line query.
STATUS_TIMEOUT_S = 10


def _changes(worktree: Path) -> list[str] | None:
    """``git status --porcelain`` lines for the worktree, or None when it fails."""
    try:
        done = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            env=git_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=STATUS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return None if done.returncode != 0 else done.stdout.splitlines()


def _unclean(worktree: Path) -> str | None:
    """Why a clean_tree stage cannot be done yet, or None when the tree is clean."""
    lines = _changes(worktree)
    if lines is None:
        return "git status failed, so the worktree may have uncommitted changes; fix it first"
    untracked = [line[3:] for line in lines if line.startswith("?? ")]
    if len(untracked) < len(lines):
        return "the worktree has uncommitted changes; commit them first"
    if untracked:
        names = ", ".join(untracked[:5]) + (", ..." if len(untracked) > 5 else "")
        return (
            f"the worktree has untracked files ({names}); commit the ones that belong to "
            "the work and add the others to .gitignore"
        )
    return None


# How many dashes the reinjected text names; the command lists them all.
_DASHES_SHOWN = 5


def _dash_base(worktree: Path, state: dict) -> str | None:
    """What the branch is measured against: origin/HEAD when the repository has
    one, else the commit the run started on. None for a run started before the
    loop recorded that commit, in a repository with no origin/HEAD."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", "origin/HEAD"],
            cwd=worktree,
            env=git_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=STATUS_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        done = None
    if done is not None and done.returncode == 0 and done.stdout.strip():
        return "origin/HEAD"
    return state.get("start_head")


def _dashes(worktree: Path, state: dict) -> str | None:
    """Why the branch fails the no-unicode-dash rule, or None when it passes."""
    base = _dash_base(worktree, state)
    if base is None:
        return None
    try:
        found = nd.gate(worktree, base)
    except nd.DashError as exc:
        return f"the no-unicode-dash check could not run ({exc}); fix it first"
    if not found:
        return None
    shown = "\n".join(f"  {v.message()}" for v in found[:_DASHES_SHOWN])
    more = f"\n  ... and {len(found) - _DASHES_SHOWN} more" if len(found) > _DASHES_SHOWN else ""
    return (
        f"the branch adds {len(found)} banned dash(es); run loop-no-dash --base {base} "
        f"and fix each one, then commit (reword a commit message that holds one)\n{shown}{more}"
    )


_URL_RE = re.compile(r"^https?://\S+$")


def _plan_path(plan: str, worktree: Path) -> Path:
    path = Path(plan).expanduser()
    return path if path.is_absolute() else worktree / path


def _block(state: dict, text: str) -> TurnEndVerdict:
    # Recorded for the event trail: the revision this block wrote. Reentrant
    # blocks are bounded by the stage's attempts, not by this field.
    state["last_block_revision"] = state["revision"] + 1
    return TurnEndVerdict(block=True, reinject=text)


def _no_token(
    run: rs.Run, state: dict, why: str, refused_on: list[str] | None = None
) -> TurnEndVerdict:
    stage = state["current_stage"]
    state["attempts"][stage] += 1
    state["total_attempts"] += 1
    if not refused_on:
        # Any other refusal breaks the run of refusals beside the same tasks.
        state["refused_on"] = []
    cap = (state.get("caps") or {}).get("attempts", rs.MAX_ATTEMPTS)
    if state["attempts"][stage] >= cap:
        release = _refusal_release(run, state, refused_on) if refused_on else ""
        rs.pause(
            state,
            "no_message",
            f"Stage {stage} has ended {state['attempts'][stage]} turns that were not "
            f"accepted (last: {why}). Read the transcript, then {run.config.resume_command} "
            f"or {run.config.abort_command}.{release}",
        )
        return TurnEndVerdict(block=False, pause_reason="no_message")
    return _block(state, f"The stage is not finished: {why}.\n\n{rs.state_text(run, state)}")


def _wait_minutes(state: dict) -> int | None:
    since = ps.parse_iso(state.get("waiting_since"))
    if since is None:
        return None
    return int((datetime.now(UTC) - since).total_seconds() // 60)


def _refusal_release(run: rs.Run, state: dict, refused_on: list[str]) -> str:
    """The card's word on tasks a refused STAGE DONE paused beside.

    One such pause says nothing about the tasks: a reviewer that still runs
    will report, and a resume keeps waiting. Tasks are marked stuck, so the
    person's resume releases them, only when the stage paused beside them a
    second time in a row and the run has waited on them for
    ``rs.STUCK_AFTER_S``: an agent can reach two pauses in seconds beside a
    reviewer that is still working, but not make a wait old. Only those tasks
    are released (``stuck_on``), never a newer task beside them.
    """
    minutes = _wait_minutes(state)
    waited = f"waited {minutes} min" if minutes is not None else "waited an unknown time"
    both = sorted(set(refused_on) & set(state.get("refused_on") or []))
    old = minutes is not None and minutes * 60 >= rs.STUCK_AFTER_S
    if both and old:
        state["wait_capped"] = True
        state["stuck_on"] = both
        state["refused_on"] = []
        return (
            f" The stage paused a second time beside {', '.join(both)}, which have {waited}. "
            "They may still be running: check the session first, then "
            f"{run.config.resume_command} releases them."
        )
    state["refused_on"] = both or sorted(refused_on)
    floor = rs.STUCK_AFTER_S // 60
    return (
        f" This resume keeps waiting on {', '.join(refused_on)} ({waited}). If the stage "
        f"pauses again beside them once they have waited {floor} min, "
        f"{run.config.resume_command} then releases them."
    )


def _gate_card(name: str, gate: str, state: dict, config: LoopConfig) -> str:
    """The card for a gate: what is being approved, the plan file the loop read and
    the loop files it unlocks, so a person approves what the run will act on."""
    plan = state.get("plan_path")
    edits = state.get("pending_loop_edits") or state.get("declared_loop_edits") or []
    lines = []
    if plan:
        lines.append(f"Plan read: {plan}")
        lines.append(
            "Loop files it unlocks once approved: " + (", ".join(edits) if edits else "none")
        )
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
            text = rs.read_plan(path)
            if text is None:
                return _no_token(
                    run, state, f"PLAN: {value} is not a readable plan file of at most 1 MB"
                )
            state["plan_path"] = str(path)
            edits = parse_loop_edits_block(text, config)
            if stage.gate != "none":
                # The plan unlocks nothing until a person approves it at the
                # gate; resume reads it again then.
                state["pending_loop_edits"] = edits
            else:
                state["declared_loop_edits"] = edits
        elif label == "PR" and value:
            if not _URL_RE.match(value):
                return _no_token(run, state, f"PR: {value} is not the pull request's url")
            state["pr_url"] = value
    unclean = _unclean(run.worktree) if stage.clean_tree else None
    if unclean:
        return _no_token(run, state, unclean)
    dashes = _dashes(run.worktree, state) if stage.clean_tree else None
    if dashes:
        return _no_token(run, state, dashes)
    state["history"].append({"at": ri.now_iso(), "event": "done", "stage": stage.name})
    if state["current"] + 1 >= len(config.stages):
        state["status"] = "done"
        state["ended_reason"] = "done"
        return PASS
    state["current"] += 1
    state["current_stage"] = config.stages[state["current"]].name
    state["attempts"][state["current_stage"]] = 0
    state.setdefault("wait_turns", {})[state["current_stage"]] = 0
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
    # A carve-out is never accepted, even under a directory the plan declared.
    accepted = [k for k in changed if declared(k, edits) and not is_carveout(k, run.config)]
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


def _record_pending(state: dict, pending: list[str], shell: tuple[str, ...] = ()) -> None:
    """What the run waits for, and since when.

    The clock starts when a wait starts and keeps running while any task is
    pending, so a task that never reports cannot hide behind newer ones. A turn
    end with nothing pending ends the wait and restarts the stage's wait count:
    the cap bounds one wait, not every review round of a stage.
    """
    if not pending:
        rs.clear_wait(state)
        state.setdefault("wait_turns", {})[state["current_stage"]] = 0
        return
    if state.get("waiting_since") is None:
        state["waiting_since"] = ri.now_iso()
    state["waiting_on"] = list(pending)
    # A wait on shell commands alone may be one that never ends, such as a dev
    # server; the supervisor flags it sooner.
    state["waiting_shell_only"] = all(t in shell for t in pending)


def _pending_rules(
    run: rs.Run, state: dict, pending: list[str], last: str
) -> TurnEndVerdict | None:
    """The verdict for a turn that ends while background tasks run, else ``None``."""
    if not pending:
        return None
    ids = ", ".join(pending)
    if last == rs.DONE_TOKEN:
        return _no_token(
            run,
            state,
            f"a background task is still running ({ids}); end your turn without a token, "
            "and its notification wakes this session. A background command that never ends "
            f"on its own, such as a dev server, is stopped with {run.config.stop_task_tool} "
            "before STAGE DONE",
            pending,
        )
    stage = state["current_stage"]
    turns = state.setdefault("wait_turns", {})
    cap = (state.get("caps") or {}).get("waits", rs.MAX_WAITS)
    if turns.get(stage, 0) >= cap:
        state["wait_capped"] = True
        rs.pause(
            state,
            "no_message",
            f"Stage {stage} waited {cap} turns on background tasks that have not reported "
            f"({ids}, since {state.get('waiting_since')}). If they will never report, "
            f"{run.config.resume_command} releases them and the stage goes on; "
            f"{run.config.abort_command} ends the run.",
        )
        return TurnEndVerdict(block=False, pause_reason="no_message")
    turns[stage] = turns.get(stage, 0) + 1
    state["history"].append(
        {"at": ri.now_iso(), "event": "wait", "stage": stage, "tasks": list(pending)}
    )
    return TurnEndVerdict(block=False, tasks=tuple(pending))


# How many other sessions a run remembers having told about adoption.
_TOLD_MAX = 20


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
        if event.activity_path:
            state["activity_path"] = event.activity_path
        released = set(state.get("released_tasks") or [])
        pending = [t for t in event.pending_tasks if t not in released]
        if not event.tasks_unknown:
            _record_pending(state, pending, event.shell_tasks)
        verdict = _check_guard(run, state)
        if verdict is not None:
            return verdict
        message = event.message or ""
        last = _last_line(message)
        if last == rs.PAUSE_TOKEN:
            rs.pause(state, "needs_human", message[-_TAIL_CHARS:], event.prompt_id)
            return TurnEndVerdict(block=False, pause_reason="needs_human")
        verdict = _pending_rules(run, state, pending, last)
        if verdict is not None:
            return verdict
        if last == rs.DONE_TOKEN and event.tasks_unknown:
            # Fail closed on the step that can harm: an advance beside a
            # reviewer that still runs. A transcript that stays unreadable
            # reaches a person at the attempts cap.
            return _no_token(
                run,
                state,
                "the loop could not read which background tasks still run; end your turn "
                "without a token if you wait on one, or end it again with the token",
            )
        if last == rs.DONE_TOKEN:
            return _advance(run, state, message)
        return _no_token(run, state, "the last line was not a token")

    def tell(state: dict) -> TurnEndVerdict:
        told = state.setdefault("told_sessions", [])
        if state["status"] != "running" or event.session_id in told:
            return PASS
        told.append(event.session_id)
        del told[:-_TOLD_MAX]
        return TurnEndVerdict(
            block=False,
            note=(
                f"delivery-loop: the run in this worktree (task {state.get('task')}, stage "
                f"{state['current_stage']}) is driven by another session, so this one is "
                f"not. After /clear or a new session, type {run.config.resume_command} "
                "here to drive it from this session."
            ),
        )

    _, state = rs.read(run)
    if state is None or state.get("status") != "running":
        return PASS
    bound = state.get("session_id")
    if bound is not None and bound != event.session_id:
        # Once per session: a /clear starts a new session id, and the run would
        # otherwise stop being driven without a word.
        if event.session_id and event.session_id not in (state.get("told_sessions") or []):
            try:
                return rs.update(run, tell, timeout)
            except ri.LockTimeout:
                # The note is a courtesy; a session that does not drive the run
                # is never blocked over it.
                return PASS
        return PASS
    return rs.update(run, apply, timeout)
