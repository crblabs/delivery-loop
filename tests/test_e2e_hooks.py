"""The shims as Claude Code runs them: a python3 subprocess, a payload on stdin.

Every other test calls the adapter in-process. These run ``hooks/*.py`` and
``bin/delivery-loop`` the way the plugin does, with a fake home and a temporary
repository, so the shim, the import path and the output shape are checked
together.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _run(args: list[str], payload: dict | None, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *args],
        input=json.dumps(payload) if payload is not None else "",
        capture_output=True,
        text=True,
        cwd=cwd,
        env=dict(os.environ),
        check=False,
        timeout=30,
    )


def _stop(cwd: Path, message: str) -> dict:
    return {
        "session_id": "s1",
        "cwd": str(cwd),
        "hook_event_name": "Stop",
        "stop_hook_active": False,
        "last_assistant_message": message,
    }


def test_a_run_from_start_to_a_guarded_edit(make_repo, tmp_path: Path) -> None:
    # Value: protects=the done-when: a new repo starts a run with no file added, and the
    # hooks drive and guard it; fails_when=any wiring between shim, adapter and core
    # breaks; why_new=plugin; seam=subprocess
    worktree = make_repo(tmp_path / "host")
    started = _run(
        [str(ROOT / "bin/delivery-loop"), "--session", "s1", "start", "CRB-1"], None, worktree
    )
    assert started.returncode == 0, started.stderr
    assert started.stdout.startswith("Stage 1/5: autoplan")
    assert not (worktree / ".claude").exists(), "start must add no file to the repository"
    stop = _run(
        [str(ROOT / "hooks/pipeline_hook.py"), "stop"], _stop(worktree, "thinking"), worktree
    )
    assert stop.returncode == 0, stop.stderr
    assert json.loads(stop.stdout)["decision"] == "block"
    edit = {
        "session_id": "s1",
        "cwd": str(worktree),
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(worktree / "loop.toml")},
    }
    guard = _run([str(ROOT / "hooks/pipeline_hook.py"), "guard"], edit, tmp_path)
    assert guard.returncode == 0, guard.stderr
    assert json.loads(guard.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"
    status = _run([str(ROOT / "bin/delivery-loop"), "status"], None, worktree)
    assert "Status: running" in status.stdout


def test_the_no_run_path_loads_nothing_heavy(tmp_path: Path) -> None:
    # Value: protects=every session on the machine pays for the hooks only a few file
    # checks; fails_when=the no-run path imports the config loader, git or the adapter;
    # why_new=user-scope plugin; seam=subprocess
    payloads = {
        "guard": {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cat <<'EOF'\n" + "x" * 200_000 + "\nEOF"},
        },
        "stop": {"session_id": "s1", "cwd": str(tmp_path), "hook_event_name": "Stop"},
        "prompt": {"session_id": "s1", "cwd": str(tmp_path), "prompt": "hello"},
    }
    for kind, payload in payloads.items():
        done = _run(
            ["-X", "importtime", str(ROOT / "hooks/pipeline_hook.py"), kind], payload, tmp_path
        )
        assert done.returncode == 0 and done.stdout == ""
        loaded = {line.rsplit("|", 1)[-1].strip() for line in done.stderr.splitlines()}
        for heavy in ("core.config", "subprocess", "tomllib", "adapters.claude_code.hooks"):
            assert heavy not in loaded, (kind, heavy)


def test_a_typed_resume_through_the_prompt_shim(make_repo, tmp_path: Path) -> None:
    worktree = make_repo(tmp_path / "host")
    started = _run(
        [str(ROOT / "bin/delivery-loop"), "--session", "s1", "start", "CRB-1"], None, worktree
    )
    assert started.returncode == 0, started.stderr
    stop = _run(
        [str(ROOT / "hooks/pipeline_hook.py"), "stop"],
        _stop(worktree, "ASK x\n<promise>NEEDS HUMAN</promise>"),
        worktree,
    )
    assert "paused this run" in json.loads(stop.stdout)["systemMessage"]
    payload = {"session_id": "s1", "cwd": str(worktree), "prompt": "/delivery-loop:pipeline resume"}
    done = _run([str(ROOT / "hooks/pipeline_hook.py"), "prompt"], payload, worktree)
    context = json.loads(done.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("delivery-loop resume: Stage 1/5")
