"""The writer of a run's state file, and the run's life outside the hooks.

``core.pipeline_state`` reads and validates a state file for the supervisor.
This module is the other half: it starts a run, applies every later change under
the run directory's lock, and keeps the run index (``core.run_index``) in step
with the state, which stays the source of truth. Every state it writes must pass
``pipeline_state.valid``, so the supervisor reads a run the hooks wrote as ``ok``.

A run records the config it started under in ``<run dir>/config.json``. The
hooks and the CLI judge the run by that snapshot only, so neither a ``--config``
given to ``start`` nor a later edit of ``loop.toml`` or the user file changes the
rules of a run in flight. The snapshot's hash is part of the guard evidence.

``abort`` is not here: ``core.run_index.abort_run`` is the one abort, so it also
works on an interpreter older than the floor.

    (none) --start--> running --stage done--> running ... --last stage done--> done
                         |  --pause / gate / no token / guard change--> awaiting_human
    awaiting_human --resume (a person)--> running
    running | awaiting_human --abort--> failed (ended_reason: aborted)
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from core import pipeline_state as ps
from core import run_index as ri
from core.config import (
    GIT_TIMEOUT_S,
    PLUGIN_STAGES_DIR,
    SETTINGS_FILES,
    ConfigError,
    LoopConfig,
    StageSpec,
    from_dict,
    git_env,
    repo_slug,
    to_dict,
)
from core.guard_evidence import config_hash, guard_map
from core.pipeline_loop_paths import parse_loop_edits_block

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
STAGES_DIR = PLUGIN_ROOT / PLUGIN_STAGES_DIR
CONFIG_SNAPSHOT = ri.SNAPSHOT_FILE
# How many turns a stage may end without a token before the run pauses.
MAX_ATTEMPTS = 3
DONE_TOKEN = "<promise>STAGE DONE</promise>"
PAUSE_TOKEN = "<promise>NEEDS HUMAN</promise>"
ACTIVE = ri.ACTIVE
_REMOTE_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")

T = TypeVar("T")


class RunError(Exception):
    """A run cannot start or change; the message says what to do."""


@dataclass(frozen=True)
class Run:
    """One active run, found through its index entry."""

    key: str
    entry: dict
    run_dir: Path
    config: LoopConfig

    @property
    def state_path(self) -> Path:
        return self.run_dir / self.config.state_file

    @property
    def worktree(self) -> Path:
        return Path(self.entry["worktree"])


def _git(worktree: Path, *args: str) -> str | None:
    """One line of git output, or ``None`` on any failure: the callers have a fallback."""
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=worktree,
            env=git_env(),
            capture_output=True,
            text=True,
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    out = done.stdout.strip()
    return out if done.returncode == 0 and out else None


def detect_repo(worktree: Path) -> str:
    """``owner/name`` from the ``origin`` remote, else ``local/<main checkout name>``."""
    url = _git(worktree, "remote", "get-url", "origin")
    match = _REMOTE_RE.search(url) if url else None
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    try:
        slug = repo_slug(worktree)
    except ConfigError:
        slug = None
    return f"local/{slug or worktree.name}"


def detect_branch(worktree: Path) -> str | None:
    return _git(worktree, "symbolic-ref", "--short", "HEAD")


def resolve_prompt(stage: StageSpec, worktree: Path, config: LoopConfig) -> Path | None:
    """The stage's prompt file: the host's own override first, then the plugin's."""
    for base in (worktree / config.stage_target, STAGES_DIR):
        candidate = base / stage.prompt
        if candidate.is_file():
            return candidate
    return None


def stage_text(
    config: LoopConfig, worktree: Path, index: int, task: str, plan: str | None = None
) -> str:
    """What the agent is told at the start of a stage, and reinjected with."""
    stage = config.stages[index]
    lines = [f"Stage {index + 1}/{len(config.stages)}: {stage.name}. Task: {task}."]
    if plan:
        lines.append(f"Approved plan: {plan}")
    if stage.command:
        lines.append(f"Run {stage.command}.")
    prompt = resolve_prompt(stage, worktree, config)
    if prompt is not None:
        lines += ["", prompt.read_text(encoding="utf-8").strip(), ""]
    if stage.emits:
        lines.append(f"Before the token, write: {stage.emits}.")
    lines.append(
        f"End the stage's last turn with the line {DONE_TOKEN}. To stop for a person, end "
        f"with a decision card (ASK, REC, ALT, COST, REPLY) and then the line {PAUSE_TOKEN}."
    )
    return "\n".join(lines)


def state_text(run: Run, state: dict) -> str:
    """The current stage's text for a run, from its state."""
    return stage_text(
        run.config, run.worktree, state["current"], state.get("task") or "", state.get("plan_path")
    )


def legacy_install(worktree: Path, config: LoopConfig) -> list[str]:
    """The in-repo hook files of the old install that its settings still wire in."""
    settings = ""
    for name in SETTINGS_FILES:
        try:
            settings += (worktree / name).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return [
        rel
        for rel in config.guard_watch
        if (worktree / rel).is_file() and Path(rel).name in settings
    ]


def _initial_state(
    worktree: Path, task: str, repo: str, config: LoopConfig, session_id: str | None, now: str
) -> dict:
    names = list(config.stage_names)
    return {
        "version": ps.SUPPORTED_VERSION,
        "run_id": str(uuid.uuid4()),
        "task": task,
        "repo": repo,
        "branch": detect_branch(worktree),
        "worktree": str(worktree),
        "stages": names,
        "current": 0,
        "current_stage": names[0],
        "status": "running",
        "attempts": dict.fromkeys(names, 0),
        "total_attempts": 0,
        "caps": {"attempts": MAX_ATTEMPTS},
        "revision": 1,
        "session_id": session_id,
        "history": [{"at": now, "event": "start", "stage": names[0]}],
        "started_at": now,
        "updated_at": now,
        "hook_seen": None,
        "paused_reason": None,
        "paused_prompt_id": None,
        "pending_question": None,
        "pending_promotion": [],
        "guard_baseline": None,
        "guard_files_seen": [],
        "guard_pending": None,
        "declared_loop_edits": [],
        "plan_path": None,
        "pr_url": None,
        "last_block_revision": None,
        "resumed_by": None,
        "ended_reason": None,
        "config_sha256": None,
        # A run the plugin's hooks drive. Only such a run must have an index
        # entry; a run an older in-repo install started has none.
        "driver": "plugin",
    }


def _session_run(session_id: str | None, worktree: Path) -> str | None:
    """The worktree of another active run this session drives, if any."""
    if not session_id or not ri._safe_id(session_id):
        return None
    entry = ri.read_entry(ri.session_path(session_id))
    if entry is None or entry.get("worktree") == str(worktree):
        return None
    state = ri.read_json_file(ri.state_file(entry["run_dir"]), ri.STATE_MAX_BYTES)
    if not isinstance(state, dict) or state.get("session_id") != session_id:
        return None
    return entry.get("worktree") if state.get("status") in ACTIVE else None


def start(
    cwd: Path, task: str, config: LoopConfig, session_id: str | None = None
) -> tuple[Run, str]:
    """Start a run in the worktree ``cwd`` is in, and return it with stage 1's text."""
    task = task.strip()
    if not task:
        raise RunError(
            "start needs a task. Cause: no task was given. "
            "Fix: delivery-loop start CRB-30 (an issue id or a short title)."
        )
    root = ri.worktree_root(str(cwd))
    if root is None:
        raise RunError(
            f"{cwd} is not in a git worktree. Cause: a run belongs to one worktree. "
            "Fix: cd into the repository (git init a new one) and start again."
        )
    worktree = Path(root)
    key = ri.worktree_key(root)
    # One lock per worktree around the whole check-then-write, so two starts in
    # one worktree cannot both pass the check, whatever state root each uses.
    with ri.locked(str(Path(ri.state_home()) / ri.INDEX_DIR / ".locks" / key), ri.PERSON_LOCK_S):
        existing = ri.read_entry(ri.index_path(key))
        if existing is not None:
            condition, state = ps.read_state(Path(ri.state_file(existing["run_dir"])), config)
            if condition != "missing" and (state is None or state.get("status") in ACTIVE):
                raise RunError(
                    f"a run is already active in {worktree}. Cause: one run per worktree. "
                    f"Fix: delivery-loop status, or ask a person to run {config.abort_command}."
                )
            ri.remove_entry(key, existing.get("session_id"))
        other = _session_run(session_id, worktree)
        if other is not None:
            raise RunError(
                f"this session already drives the run in {other}. Cause: one session drives "
                f"one run; its turn ends would drive only one of them. Fix: finish or have a "
                f"person run {config.abort_command} there, or start from another session."
            )
        legacy = legacy_install(worktree, config)
        if legacy:
            raise RunError(
                "this worktree still has the in-repo install: "
                + ", ".join(legacy)
                + ". Cause: those hooks would drive the run a second time. Fix: delete them and "
                f"their entries in {SETTINGS_FILES[0]}, then start again. See "
                f"{ri.README_URL}moving-from-the-in-repo-install"
            )
        missing = [s.name for s in config.stages if resolve_prompt(s, worktree, config) is None]
        if missing:
            raise RunError(
                f"no prompt file for stage(s) {', '.join(missing)}. Cause: a stage's prompt is "
                f"read from {config.stage_target} in the repository, then from the plugin. "
                "Fix: add the file or set the stage's prompt key in loop.toml."
            )
        repo = detect_repo(worktree)
        run_dir = ps.run_dir(worktree, repo, config)
        with ri.locked(str(run_dir), ri.PERSON_LOCK_S):
            ps.prepare_run_dir(worktree, repo, config)
            path = run_dir / config.state_file
            old = ri.read_json_file(str(path), ri.STATE_MAX_BYTES)
            if path.exists():
                run_id = old.get("run_id") if isinstance(old, dict) else None
                # The old file's own run id names its copy, so it must be one.
                safe = run_id if isinstance(run_id, str) and ps.UUID_RE.match(run_id) else "old"
                path.replace(run_dir / f"{Path(config.state_file).stem}.{safe}.json")
            ri.write_json_atomic(str(run_dir / CONFIG_SNAPSHOT), to_dict(config))
            now = ri.now_iso()
            state = _initial_state(worktree, task, repo, config, session_id, now)
            state["config_sha256"] = config_hash(run_dir)
            baseline = guard_map(worktree, config, run_dir)
            state["guard_baseline"] = baseline
            state["guard_files_seen"] = [baseline]
            if not ps.valid(state, config):
                raise RunError("internal error: the initial state does not validate")
            ri.write_json_atomic(str(path), state)
            ri.write_entry(key, str(run_dir), str(worktree), session_id)
            ri.append_event(
                str(run_dir),
                {"hook": "cli", "decision": "start", "task": task, "session": session_id},
            )
    run = Run(key, ri.read_entry(ri.index_path(key)) or {}, run_dir, config)
    return run, stage_text(config, worktree, 0, task)


def open_run(key: str, entry: dict) -> Run:
    """The run an index entry names, judged by the config it started under."""
    run_dir = Path(entry["run_dir"])
    snapshot = ri.read_json_file(str(run_dir / CONFIG_SNAPSHOT), ri.SNAPSHOT_MAX_BYTES)
    if snapshot is None:
        raise RunError(
            f"the run in {entry.get('worktree')} has no readable {CONFIG_SNAPSHOT}. "
            "Cause: its run directory was edited or removed. Fix: delivery-loop abort, then "
            "start again."
        )
    try:
        config = from_dict(snapshot)
    except ConfigError as exc:
        raise RunError(f"the run's config snapshot is not valid: {exc}") from exc
    if not isinstance(entry.get("worktree"), str):
        raise RunError("the run's index entry names no worktree. Fix: delivery-loop abort.")
    return Run(key, entry, run_dir, config)


def find(cwd: object, target: object = None, session_id: object = None) -> Run | None:
    found = ri.find_entry(cwd, target, session_id)
    return None if found is None else open_run(*found)


def read(run: Run) -> tuple[str, dict | None]:
    return ps.read_state(run.state_path, run.config)


def sync_index(run: Run, state: dict) -> None:
    """Make the index say what the state says, dropping a session the run left."""
    previous = ri.read_entry(ri.index_path(run.key)) or {}
    if previous.get("session_id") not in (None, state.get("session_id")):
        ri.remove_session(previous["session_id"])
    if state.get("status") in ACTIVE:
        ri.write_entry(run.key, str(run.run_dir), str(run.worktree), state.get("session_id"))
    else:
        ri.remove_entry(run.key, state.get("session_id"))
        ri.remove_entry(run.key, run.entry.get("session_id"))


def update(run: Run, mutate: Callable[[dict], T], timeout: float = ri.PERSON_LOCK_S) -> T:
    """Apply ``mutate`` to the state under the lock, then write and re-index it.

    ``mutate`` changes the dict in place and returns what the caller needs. The
    write bumps ``revision`` and ``updated_at`` once, and refuses a state the
    supervisor would read as ``corrupt``.
    """
    with ri.locked(str(run.run_dir), timeout):
        condition, state = read(run)
        if state is None:
            raise RunError(
                f"the run's state is {condition}. Cause: {run.state_path} is missing or does not "
                f"validate. Fix: {run.config.abort_command}, then start again."
            )
        before = state["revision"]
        result = mutate(state)
        state["revision"] = before + 1
        state["updated_at"] = ri.now_iso()
        if not ps.valid(state, run.config):
            raise RunError("internal error: the state no longer validates; nothing was written")
        ri.write_json_atomic(str(run.state_path), state)
        sync_index(run, state)
    return result


def pause(state: dict, reason: str, question: str, prompt_id: object = None) -> None:
    state["status"] = "awaiting_human"
    state["paused_reason"] = reason
    state["pending_question"] = question
    state["paused_prompt_id"] = prompt_id if isinstance(prompt_id, str) else None
    state["history"].append(
        {"at": ri.now_iso(), "event": "pause", "reason": reason, "stage": state["current_stage"]}
    )


# A plan is prose; anything larger is not one.
MAX_PLAN_BYTES = 1 << 20


def read_plan(path: Path) -> str | None:
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


def _approve_plan(run: Run, state: dict) -> None:
    """At a gate, unlock the loop files the plan declares as it reads now."""
    plan = state.get("plan_path")
    text = read_plan(Path(plan)) if plan else None
    state["declared_loop_edits"] = parse_loop_edits_block(text, run.config) if text else []
    state.pop("pending_loop_edits", None)


def resume(run: Run, by: str, session_id: str | None = None) -> str:
    """Resume a paused run for a person, and return the text the agent continues with.

    Only a person's own action reaches this: the harness's prompt hook, or the
    CLI in a terminal. A guard pause accepts the changed files as the new
    baseline. Any other pause restarts the current stage's attempts.
    ``session_id`` comes from the harness, never from an argument the agent can
    type: given from another session, it binds the run to that session, which is
    how a person adopts a run whose session ended, running or paused.
    """

    def apply(state: dict) -> str:
        adopting = session_id is not None and session_id != state.get("session_id")
        if adopting:
            state["history"].append(
                {"at": ri.now_iso(), "event": "adopt", "from": state.get("session_id")}
            )
            state["session_id"] = session_id
        if state["status"] == "running" and adopting:
            state["attempts"][state["current_stage"]] = 0
            return state_text(run, state)
        if state["status"] != "awaiting_human":
            raise RunError(
                f"nothing to resume: the run is {state['status']}. Fix: delivery-loop status."
            )
        if state.get("paused_reason") == "guard_changed" and isinstance(
            state.get("guard_pending"), dict
        ):
            state["guard_baseline"] = state["guard_pending"]
            state["guard_files_seen"].append(state["guard_pending"])
        if state.get("paused_reason") == "gate":
            _approve_plan(run, state)
        state["guard_pending"] = None
        state["status"] = "running"
        state["attempts"][state["current_stage"]] = 0
        state["resumed_by"] = by
        state["history"].append(
            {"at": ri.now_iso(), "event": "resume", "by": by, "reason": state["paused_reason"]}
        )
        state["paused_reason"] = None
        state["pending_question"] = None
        return state_text(run, state)

    text = update(run, apply)
    ri.append_event(
        str(run.run_dir), {"hook": "cli", "decision": "resume", "by": by, "session": session_id}
    )
    return text


def status_text(run: Run) -> str:
    condition, state = read(run)
    if state is None:
        return f"Run in {run.worktree}: state {condition}. Fix: delivery-loop abort."
    stage = f"{state['current'] + 1}/{len(state['stages'])} {state['current_stage']}"
    lines = [
        f"Task {state.get('task')} in {run.worktree}",
        f"Status: {state['status']}, stage {stage}, revision {state['revision']}",
    ]
    if state["status"] == "awaiting_human":
        lines.append(f"Paused: {state.get('paused_reason')}")
        if state.get("paused_reason") == "guard_changed":
            baseline = state.get("guard_baseline") or {}
            pending = state.get("guard_pending") or {}
            keys = set(baseline) | set(pending)
            changed = sorted(k for k in keys if baseline.get(k) != pending.get(k))
            lines.append("Changed guard files: " + (", ".join(changed) or "none recorded"))
            lines.append(f"Resume accepts them: {run.config.resume_command}")
        elif state.get("pending_question"):
            lines.append(state["pending_question"].strip()[-500:])
    if state.get("plan_path"):
        edits = state.get("declared_loop_edits") or []
        lines.append(f"Plan: {state['plan_path']}")
        lines.append("Loop files it unlocks: " + (", ".join(edits) if edits else "none"))
    lines.append(f"Run directory: {run.run_dir}")
    events = ri.read_events(str(run.run_dir), 5)
    if events:
        lines.append("Last events:")
        lines += ["  " + json.dumps(e, sort_keys=True) for e in events]
    return "\n".join(lines)
