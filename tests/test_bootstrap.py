"""The Claude Code bootstrap: what the hooks and the CLI decide before the floor."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from adapters.claude_code import bootstrap as boot
from core import run_index as ri


def test_a_deeply_nested_payload_is_no_payload() -> None:
    # Value: protects=the shims never crash before their fail-open or fail-closed path;
    # fails_when=payload_of lets RecursionError out; why_new=review testing finding; seam=none
    assert boot.payload_of("[" * 100_000) is None
    assert boot.payload_of("[]") is None
    assert boot.payload_of('{"a": 1}') == {"a": 1}


def test_the_output_shapes_are_what_the_harness_reads() -> None:
    deny = json.loads(boot.deny_json("no"))["hookSpecificOutput"]
    assert deny == {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": "no",
    }
    assert json.loads(boot.block_json("go")) == {"decision": "block", "reason": "go"}
    context = json.loads(boot.context_json("note"))["hookSpecificOutput"]
    assert context == {"hookEventName": "UserPromptSubmit", "additionalContext": "note"}


@pytest.mark.parametrize(
    ("tool", "tool_input", "sub"),
    [
        ("Bash", {"command": "python3 -m adapters.claude_code.cli resume"}, "resume"),
        ("Bash", {"command": "python3 adapters/claude_code/cli.py abort"}, "abort"),
        ("Skill", {"skill": "delivery-loop:pipeline", "args": "--session x resume"}, "resume"),
        ("Skill", {"skill": "delivery-loop:pipeline", "args": "status"}, None),
        ("Skill", {"skill": "other:pipeline", "args": "resume"}, None),
    ],
)
def test_every_route_to_resume_or_abort_is_recognised(tool, tool_input, sub) -> None:
    assert boot.human_only_call(tool, tool_input) == sub


@pytest.mark.parametrize(
    ("tool", "kind"),
    [
        ("Edit", "edit"),
        ("NotebookEdit", "edit"),
        ("Bash", "shell"),
        ("mcp__filesystem__write_file", "edit"),
        ("mcp__fs__move_file", "edit"),
        ("mcp__filesystem__read_file", "other"),
        ("mcp__github__push_files", "publish"),
        ("mcp__github__create_or_update_file", "publish"),
        ("mcp__plugin_github_github__merge_pull_request", "publish"),
        ("mcp__git__git_commit", "other"),
        ("mcp__github__get_file_contents", "other"),
        ("mcp__github__list_branches", "other"),
        ("mcp__github__get_pull_request", "other"),
        ("mcp__github__list_commits", "other"),
        ("mcp__github__get_latest_release", "other"),
        ("mcp__plugin_github_github__pull_request_read", "other"),
        ("mcp__plugin_github_github__pull_request_write", "publish"),
        ("mcp__github__create_pull_request", "publish"),
        ("mcp__bitbucket__bb_add_branch", "publish"),
        ("mcp__github__pushFiles", "publish"),
        ("mcp__github__createBranch", "publish"),
        ("mcp__gitlab__create-branch", "publish"),
        ("mcp__github__commit_files", "publish"),
        ("mcp__github__upload_file", "publish"),
        ("mcp__github__get_commit", "other"),
        ("mcp__github__list_forks", "other"),
        ("Read", "other"),
    ],
)
def test_an_mcp_tool_that_writes_is_an_edit(tool: str, kind: str) -> None:
    # Value: protects=an MCP write tool cannot reach a carve-out unchecked;
    # fails_when=only the built-in edit tools are guarded; why_new=review security; seam=none
    assert boot.kind_of(tool) == kind


def test_the_target_of_an_mcp_write_is_its_path_field() -> None:
    payload = {"tool_name": "mcp__fs__write_file", "tool_input": {"path": "/x/loop.toml"}}
    assert boot.target_of(payload) == "/x/loop.toml"
    assert boot.target_of({"tool_name": "Read", "tool_input": {"file_path": "/x"}}) is None


@pytest.mark.parametrize(
    ("prompt", "sub"),
    [
        ("/delivery-loop:pipeline resume", "resume"),
        ("  /delivery-loop:pipeline abort  ", "abort"),
        ("/delivery-loop:pipeline status", None),
        ("/pipeline resume", None),
        ("/delivery-loop:pipeline Resume", "resume"),
        ("/other:pipeline resume", None),
        ("/delivery-loop:pipeline", None),
        ("please /delivery-loop:pipeline resume", None),
        ("resume the run", None),
        (None, None),
    ],
)
def test_a_typed_resume_or_abort_is_recognised(prompt, sub) -> None:
    assert boot.prompt_command(prompt) == sub


def test_old_python_aborts_from_a_typed_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Value: protects=a person on an old python3 can still end a run from Claude Code;
    # fails_when=the prompt path needs the floor; why_new=review adversarial; seam=none
    (tmp_path / ".git").mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = {"status": "running", "revision": 1, "history": []}
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    ri.write_entry(ri.worktree_key(str(tmp_path)), str(run_dir), str(tmp_path), "s1")
    monkeypatch.setattr(sys, "version_info", (3, 9, 0, "final", 0))
    payload = {"cwd": str(tmp_path), "session_id": "s1"}
    resume = boot.old_python_hook("prompt", {**payload, "prompt": "/delivery-loop:pipeline resume"})
    assert "needs python3" in resume["context"]
    abort = boot.old_python_hook("prompt", {**payload, "prompt": "/delivery-loop:pipeline abort"})
    assert "Aborted" in abort["context"]
    assert json.loads((run_dir / "state.json").read_text())["status"] == "failed"
    assert boot.old_python_hook("prompt", {**payload, "prompt": "hello"}) is None


def test_old_python_reports_an_abort_that_cannot_take_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    # Value: protects=a person on an old python3 is told why an abort did not happen;
    # fails_when=a held lock surfaces as a traceback; why_new=maintainability review;
    # seam=none
    (tmp_path / ".git").mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "state.json").write_text('{"status": "running"}', encoding="utf-8")
    ri.write_entry(ri.worktree_key(str(tmp_path)), str(run_dir), str(tmp_path), "s1")
    monkeypatch.setattr(sys, "version_info", (3, 9, 0, "final", 0))
    monkeypatch.setattr(
        ri, "abort_run", lambda *a, **k: (_ for _ in ()).throw(ri.LockTimeout("held"))
    )
    payload = {"cwd": str(tmp_path), "session_id": "s1", "prompt": "/delivery-loop:pipeline abort"}
    assert "could not abort the run: held" in boot.old_python_hook("prompt", payload)["context"]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ri, "in_terminal", lambda: True)
    assert boot.old_python_cli(["--session=s1", "abort"]) == 2
    assert "could not abort the run: held" in capsys.readouterr().err


def test_old_python_status_through_the_slash_form(tmp_path, monkeypatch, capsys) -> None:
    import io

    (tmp_path / ".git").mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = {"status": "running", "current_stage": "qa", "task": "T"}
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    ri.write_entry(ri.worktree_key(str(tmp_path)), str(run_dir), str(tmp_path), "s1")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "version_info", (3, 9, 0, "final", 0))
    monkeypatch.setattr("sys.stdin", io.StringIO("status\n"))
    assert boot.old_python_cli(["--session", "s1", "--args-stdin"]) == 0
    assert "Status: running, stage qa" in capsys.readouterr().out
