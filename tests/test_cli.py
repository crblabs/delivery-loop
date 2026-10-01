"""``delivery-loop``: the control CLI a person and the slash command run."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from adapters.claude_code import cli
from core import run_index as ri
from core import run_state as rs


def test_no_subcommand_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert "start" in capsys.readouterr().out


def test_start_without_a_task_shows_an_example(
    make_repo, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(make_repo(tmp_path / "host"))
    assert cli.main(["start"]) == 2
    assert "start CRB-30" in capsys.readouterr().err


def test_start_prints_stage_one_and_binds_the_session(
    make_repo, tmp_path: Path, monkeypatch, capsys
) -> None:
    worktree = make_repo(tmp_path / "host")
    monkeypatch.chdir(worktree)
    assert cli.main(["--session", "s7", "start", "CRB-1", "fix", "it"]) == 0
    assert capsys.readouterr().out.startswith("Stage 1/5: autoplan. Task: CRB-1 fix it.")
    assert rs.read(rs.find(str(worktree)))[1]["session_id"] == "s7"


def test_start_uses_the_agents_intent_when_no_session_is_given(
    make_repo, tmp_path: Path, monkeypatch
) -> None:
    worktree = make_repo(tmp_path / "host")
    monkeypatch.chdir(worktree)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    ri.write_intent(ri.worktree_key(str(worktree)), "s8")
    assert cli.main(["start", "CRB-1"]) == 0
    assert rs.read(rs.find(str(worktree)))[1]["session_id"] == "s8"


def test_an_unbound_start_outside_a_terminal_is_refused(
    make_repo, tmp_path: Path, monkeypatch, capsys
) -> None:
    # Value: protects=a run goes to the session that started it; fails_when=a start from a
    # script binds the first session that ends a turn in the worktree; why_new=red-team
    # review; seam=none
    worktree = make_repo(tmp_path / "host")
    monkeypatch.chdir(worktree)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setattr(ri, "in_terminal", lambda: False)
    assert cli.main(["start", "CRB-1"]) == 2
    assert "could not tell which Claude Code session" in capsys.readouterr().err
    assert rs.find(str(worktree)) is None
    # The slash command still starts when the harness leaves its session empty.
    monkeypatch.setattr("sys.stdin", io.StringIO("start CRB-1\n"))
    assert cli.main(["--session", "", "--args-stdin"]) == 0


def test_start_refuses_a_bad_config(make_repo, tmp_path: Path, monkeypatch, capsys) -> None:
    worktree = make_repo(tmp_path / "host")
    (worktree / "loop.toml").write_text("[harness\n", encoding="utf-8")
    monkeypatch.chdir(worktree)
    assert cli.main(["start", "CRB-1"]) == 2
    assert "CONFIG_INVALID" in capsys.readouterr().err
    assert cli.main(["start", "CRB-1", "--config", str(tmp_path / "none.toml")]) == 2


def test_start_with_a_named_config_moves_the_run_but_not_the_index(
    make_repo, tmp_path: Path, monkeypatch
) -> None:
    # Value: protects=hooks find a run started under --config that moves state_root;
    # fails_when=the index follows state_root; why_new=spec review; seam=none
    # DELIVERY_LOOP_HOME outranks a named file, so leave the default home in place.
    monkeypatch.delenv(ri.HOME_ENV)
    worktree = make_repo(tmp_path / "host")
    named = tmp_path / "named.toml"
    named.write_text(f'[harness]\nstate_root = "{tmp_path / "moved"}"\n', encoding="utf-8")
    monkeypatch.chdir(worktree)
    assert cli.main(["--session", "s1", "start", "CRB-1", "--config", str(named)]) == 0
    entry = ri.find_entry(str(worktree))[1]
    assert entry["run_dir"].startswith(str(tmp_path / "moved"))
    assert Path(ri.index_path(ri.worktree_key(str(worktree)))).is_relative_to(ri.state_home())
    assert rs.read(rs.find(str(worktree)))[0] == "ok"


def test_status_resume_and_abort(start_run, monkeypatch, capsys) -> None:
    run, worktree = start_run()
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(ri, "in_terminal", lambda: True)
    assert cli.main(["status"]) == 0
    assert "Status: running" in capsys.readouterr().out
    assert cli.main(["resume"]) == 2
    assert "nothing to resume" in capsys.readouterr().err
    rs.update(run, lambda s: rs.pause(s, "needs_human", "ASK x"))
    assert cli.main(["resume"]) == 0
    assert capsys.readouterr().out.startswith("Stage 1/5")
    assert cli.main(["abort"]) == 0
    assert "Aborted" in capsys.readouterr().out
    assert cli.main(["status"]) == 0
    assert "No active delivery-loop run" in capsys.readouterr().out


def test_abort_with_no_run_says_so(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ri, "in_terminal", lambda: True)
    assert cli.main(["abort"]) == 0
    assert "No active" in capsys.readouterr().out


@pytest.mark.parametrize("sub", ["resume", "abort"])
def test_resume_and_abort_refuse_outside_a_terminal(start_run, monkeypatch, capsys, sub) -> None:
    # Value: protects=the agent cannot clear or end its own pause through the CLI, however
    # it spells the call; fails_when=the CLI trusts its caller; why_new=review security;
    # seam=none
    run, worktree = start_run()
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(ri, "in_terminal", lambda: False)
    rs.update(run, lambda s: rs.pause(s, "needs_human", "ASK x"))
    assert cli.main(["--session", "fake", sub]) == 2
    assert "is for a person" in capsys.readouterr().err
    state = rs.read(run)[1]
    assert state["status"] == "awaiting_human"
    assert state["session_id"] == "s1"


def test_the_slash_command_defers_resume_to_the_prompt_hook(start_run, monkeypatch, capsys) -> None:
    run, worktree = start_run()
    monkeypatch.chdir(worktree)
    monkeypatch.setattr("sys.stdin", io.StringIO("resume\n"))
    rs.update(run, lambda s: rs.pause(s, "needs_human", "ASK x"))
    assert cli.main(["--session", "s1", "--args-stdin"]) == 0
    assert "applied by the plugin's prompt hook" in capsys.readouterr().out
    assert rs.read(run)[1]["status"] == "awaiting_human"


def test_a_task_title_on_stdin_is_kept_as_typed(make_repo, tmp_path, monkeypatch, capsys) -> None:
    # Value: protects=a task with an apostrophe, quotes or $(...) starts as typed;
    # fails_when=the words are split or expanded; why_new=review QA probe; seam=none
    worktree = make_repo(tmp_path / "host")
    monkeypatch.chdir(worktree)
    title = """CRB-9 fix the user's "draft" $(not a command)"""
    monkeypatch.setattr("sys.stdin", io.StringIO(f"start {title}\n"))
    assert cli.main(["--session", "s1", "--args-stdin"]) == 0
    assert f"Task: {title}." in capsys.readouterr().out
    assert rs.read(rs.find(str(worktree)))[1]["task"] == title


@pytest.fixture
def hook_python(monkeypatch):
    """The python3 a hook would run with, fixed, so doctor does not read the host's."""
    monkeypatch.setattr(cli, "_hook_python", lambda: ("/usr/bin/python3", (3, 12)))


def test_doctor_passes_in_a_repository_and_names_the_cli(
    make_repo, tmp_path: Path, monkeypatch, capsys, hook_python
) -> None:
    monkeypatch.chdir(make_repo(tmp_path / "host"))
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "OK    git worktree" in out
    assert "alias delivery-loop=" in out
    # No gstack skills under this test's home: each stage command is a warning, not a failure.
    assert "WARN  stage autoplan: /autoplan not found" in out


def test_doctor_finds_an_installed_skill(
    make_repo, tmp_path: Path, monkeypatch, capsys, hook_python
) -> None:
    skill = Path.home() / ".claude/skills/autoplan/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: autoplan\n---\n", encoding="utf-8")
    monkeypatch.chdir(make_repo(tmp_path / "host"))
    cli.main(["doctor"])
    assert "stage autoplan" not in capsys.readouterr().out


def test_doctor_fails_outside_a_repository_and_on_a_legacy_install(
    make_repo, tmp_path: Path, monkeypatch, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["doctor"]) == 1
    assert "FAIL  git worktree" in capsys.readouterr().out
    worktree = make_repo(tmp_path / "host")
    (worktree / ".claude/hooks").mkdir(parents=True)
    (worktree / ".claude/hooks/pipeline_guard.py").write_text("#", encoding="utf-8")
    (worktree / ".claude/settings.json").write_text(
        json.dumps({"hooks": {"PreToolUse": ["pipeline_guard.py"]}}), encoding="utf-8"
    )
    monkeypatch.chdir(worktree)
    assert cli.main(["doctor"]) == 1
    assert "FAIL  legacy install" in capsys.readouterr().out


def test_doctor_warns_on_the_old_command_names(
    make_repo, tmp_path: Path, monkeypatch, capsys
) -> None:
    worktree = make_repo(tmp_path / "host")
    (worktree / "loop.toml").write_text('[commands]\nresume = "/pipeline resume"\n', "utf-8")
    monkeypatch.chdir(worktree)
    cli.main(["doctor"])
    assert "config still names /pipeline resume" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("found", "level"),
    [(("/usr/bin/python3", (3, 9)), "FAIL"), (("missing", None), "FAIL"), (("/p", (3, 11)), "OK")],
)
def test_doctor_checks_the_python3_hooks_run_with(
    make_repo, tmp_path, monkeypatch, capsys, found, level
) -> None:
    monkeypatch.setattr(cli, "_hook_python", lambda: found)
    monkeypatch.chdir(make_repo(tmp_path / "host"))
    cli.main(["doctor"])
    assert f"{level:4}  hook python3" in capsys.readouterr().out


def test_a_terminal_start_ignores_an_agents_leftover_intent(
    make_repo, tmp_path, monkeypatch
) -> None:
    # Value: protects=a person's own start is not bound to the agent's session by an
    # intent left from a declined call; fails_when=the intent outranks a terminal start;
    # why_new=adversarial review; seam=none
    worktree = make_repo(tmp_path / "host")
    monkeypatch.chdir(worktree)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    ri.write_intent(ri.worktree_key(str(worktree)), "agent")
    monkeypatch.setattr(ri, "in_terminal", lambda: True)
    assert cli.main(["start", "CRB-1"]) == 0
    assert rs.read(rs.find(str(worktree)))[1]["session_id"] is None
    assert ri.take_intent(ri.worktree_key(str(worktree))) is None


def test_start_prefers_the_harness_intent_over_a_typed_session(
    make_repo, tmp_path, monkeypatch
) -> None:
    # Value: protects=the agent cannot bind its run to a session that never ends a turn;
    # fails_when=--session outranks the intent the guard recorded; why_new=re-review;
    # seam=none
    worktree = make_repo(tmp_path / "host")
    monkeypatch.chdir(worktree)
    ri.write_intent(ri.worktree_key(str(worktree)), "real")
    assert cli.main(["--session", "fake", "start", "CRB-1"]) == 0
    assert rs.read(rs.find(str(worktree)))[1]["session_id"] == "real"


def test_a_terminal_resume_is_recorded_as_such(start_run, monkeypatch) -> None:
    run, worktree = start_run()
    monkeypatch.chdir(worktree)
    monkeypatch.setattr(ri, "in_terminal", lambda: True)
    rs.update(run, lambda s: rs.pause(s, "needs_human", "ASK x"))
    assert cli.main(["resume"]) == 0
    assert rs.read(run)[1]["resumed_by"] == "terminal"


def test_a_config_named_through_the_slash_form_is_used(
    make_repo, tmp_path, monkeypatch, capsys
) -> None:
    # Value: protects=--config typed in the slash form reaches start, and the task stays
    # as typed; fails_when=it becomes part of the task title; why_new=final re-review;
    # seam=none
    worktree = make_repo(tmp_path / "host")
    named = tmp_path / "two.toml"
    named.write_text('[[stages]]\nname = "implement"\n', encoding="utf-8")
    monkeypatch.chdir(worktree)
    monkeypatch.setattr("sys.stdin", io.StringIO(f"start CRB-2 it's small --config {named}\n"))
    assert cli.main(["--session", "s1", "--args-stdin"]) == 0
    assert capsys.readouterr().out.startswith("Stage 1/1: implement. Task: CRB-2 it's small.")
