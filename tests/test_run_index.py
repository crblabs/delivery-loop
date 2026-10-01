"""The run index, and the paths that must work on an interpreter older than the floor."""

from __future__ import annotations

import ast
import json
import os
import sys
import time
from pathlib import Path

import pytest

from adapters.claude_code import bootstrap as boot
from core import pipeline_state as ps
from core import run_index as ri

ROOT = Path(__file__).resolve().parent.parent
OLD = (
    "core/run_index.py",
    "adapters/claude_code/bootstrap.py",
    "hooks/pipeline_hook.py",
    "bin/delivery-loop",
)


@pytest.mark.parametrize("rel", OLD)
def test_the_old_python_paths_parse_on_3_8(rel: str) -> None:
    # The core half names no harness; the adapter half holds the tool names.
    # Value: protects=a person whose python3 is older than the floor can still end a run;
    # fails_when=a 3.9+ construct lands in these files; why_new=old-python path; seam=none
    ast.parse((ROOT / rel).read_text(encoding="utf-8"), feature_version=(3, 8))


def test_the_index_lives_where_the_state_reader_looks() -> None:
    assert ps.HOME_ENV == ri.HOME_ENV
    assert Path(ri.state_home()) == ps.state_home()


def test_the_worktree_key_matches_the_run_directory_hash(tmp_path: Path) -> None:
    worktree = tmp_path / "wt"
    worktree.mkdir()
    assert ps.run_dir(worktree, "o/r").name.endswith("-" + ri.worktree_key(str(worktree)))


def test_the_root_is_the_first_git_entry_walking_up(tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    inner = outer / "vendor" / "inner"
    (inner / "src").mkdir(parents=True)
    (outer / ".git").mkdir()
    (inner / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    assert ri.worktree_root(str(outer / "vendor")) == str(outer.resolve())
    assert ri.worktree_root(str(inner / "src")) == str(inner.resolve())
    assert ri.worktree_root(str(inner / "src" / "missing.py")) == str(inner.resolve())
    assert ri.worktree_root(str(tmp_path)) is None


def test_find_prefers_the_session_then_the_target_then_cwd(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for wt in (a, b):
        (wt / ".git").mkdir(parents=True)
    ri.write_entry(ri.worktree_key(str(a)), "/runs/a", str(a), "sa")
    ri.write_entry(ri.worktree_key(str(b)), "/runs/b", str(b), None)
    assert ri.find_entry(str(b), None, "sa")[1]["run_dir"] == "/runs/a"
    assert ri.find_entry(str(tmp_path), str(b / "x.py"), "zz")[1]["run_dir"] == "/runs/b"
    assert ri.find_entry(str(a), None, None)[1]["run_dir"] == "/runs/a"
    assert ri.find_entry(str(tmp_path), None, None) is None
    # A relative target is read against cwd.
    assert ri.find_entry(str(b), "x.py", None)[1]["run_dir"] == "/runs/b"


def test_a_session_id_that_is_not_a_name_is_ignored(tmp_path: Path) -> None:
    ri.write_entry("k", "/runs/a", str(tmp_path), "../escape")
    assert not (Path(ri.state_home()) / "index" / "escape").exists()
    assert ri.find_entry(None, None, "../escape") is None


def test_an_entry_that_is_not_an_absolute_run_dir_is_no_entry(tmp_path: Path) -> None:
    path = Path(ri.index_path("k"))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"run_dir": "relative"}), encoding="utf-8")
    assert ri.read_entry(str(path)) is None
    path.write_text("not json", encoding="utf-8")
    assert ri.read_entry(str(path)) is None


def test_a_symlinked_entry_is_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "t.json"
    target.write_text(json.dumps({"run_dir": "/runs/a"}), encoding="utf-8")
    path = Path(ri.index_path("k"))
    path.parent.mkdir(parents=True)
    path.symlink_to(target)
    assert ri.read_entry(str(path)) is None


def test_an_intent_is_used_once_and_only_while_fresh() -> None:
    ri.write_intent("k", "s1")
    assert ri.take_intent("k") == "s1"
    assert ri.take_intent("k") is None
    ri.write_intent("k", "s2")
    assert ri.take_intent("k", now=time.time() + ri.INTENT_TTL_S + 1) is None


@pytest.mark.parametrize(
    ("command", "calls"),
    [
        ("delivery-loop start CRB-1", [["start", "CRB-1"]]),
        ("cd x && delivery-loop resume", [["resume"]]),
        ("echo hi; FOO=1 delivery-loop abort", [["abort"]]),
        ("ls | env delivery-loop status", [["status"]]),
        ("python3 /p/bin/delivery-loop abort", [["abort"]]),
        ('"/c/plugins/x/bin/delivery-loop" resume', [["resume"]]),
        ("echo delivery-loop resume", []),
        ("command delivery-loop doctor", [["doctor"]]),
        ("echo hi", []),
    ],
)
def test_the_cli_is_found_in_a_shell_line(command: str, calls: list) -> None:
    assert ri.cli_calls(command) == calls


@pytest.mark.parametrize(
    ("command", "sub"),
    [
        ("delivery-loop resume", "resume"),
        ("delivery-loop --session x resume", "resume"),
        ("delivery-loop --session=x abort", "abort"),
        ('bash -c "delivery-loop resume"', "resume"),
        ("sh -c 'delivery-loop abort'", "abort"),
        ("echo $(delivery-loop resume)", "resume"),
        ("`delivery-loop resume`", "resume"),
        ("sudo delivery-loop resume", "resume"),
        ("xargs delivery-loop resume", "resume"),
        ("(delivery-loop resume)", "resume"),
        ("{ delivery-loop resume; }", "resume"),
        ("if true; then delivery-loop resume; fi", "resume"),
        ("timeout 5 delivery-loop abort", "abort"),
        ("python3 -u bin/delivery-loop resume", "resume"),
        ("delivery-loop status", None),
        ("delivery-loop status; echo abort", None),
        ("grep -n abort bin/delivery-loop", None),
        ("git commit -m 'fix the resume path'", None),
        ('claude -p "/delivery-loop:pipeline resume"', "resume"),
        ("python3 -m pytest /w/delivery-loop-90cd/tests -k resume", None),
        ('git commit -m "delivery-loop: fix the abort path"', None),
        ("cd /src/delivery-loop && make abort", None),
    ],
)
def test_resume_or_abort_is_found_in_any_shell_form(command: str, sub) -> None:
    # Value: protects=the early refusal of an agent's own resume; fails_when=an option,
    # a wrapper or a subshell hides the subcommand; why_new=review security finding;
    # seam=none
    assert ri.human_only_command(command) == sub


@pytest.mark.parametrize(
    ("command", "sub"),
    [
        ("delivery-loop --sess abc resume", "resume"),
        ("delivery-loop --s abc resume", "resume"),
        ("delivery-loop --se abc abort", "abort"),
        ("delivery-loop --a resume", "resume"),
        ('eval "delivery-loop resume"', "resume"),
        ("eval delivery-loop abort", "abort"),
        ("builtin eval 'delivery-loop resume'", "resume"),
        ('bash <<<"delivery-loop resume"', "resume"),
        ("bash <<< 'delivery-loop resume'", "resume"),
        ("printf 'delivery-loop resume' | sh", "resume"),
        ('echo "delivery-loop abort" | /bin/bash', "abort"),
        ("printf 'delivery-loop resume' | bash -s", "resume"),
        ("printf 'delivery-loop resume' | tee x | sh", "resume"),
        ("echo 'delivery-loop resume' | xargs -I{} sh -c '{}'", "resume"),
        ("echo 'delivery-loop resume' | source /dev/stdin", "resume"),
        ("echo 'delivery-loop resume' | sudo bash", "resume"),
        ("echo 'delivery-loop resume' |& sh", "resume"),
        ("bash<<<'delivery-loop resume'", "resume"),
        ("script -q /dev/null <<< 'delivery-loop resume'", "resume"),
        ("echo 'delivery-loop resume' | script -q /dev/null", "resume"),
        ("echo 'delivery-loop resume' | bash -o pipefail", "resume"),
        ("echo 'delivery-loop resume' | bash -s arg1", "resume"),
        ("echo 'delivery-loop resume' | bash /dev/fd/0", "resume"),
        ("echo 'delivery-loop resume' | sudo -u root bash", "resume"),
        ("echo 'delivery-loop resume' | timeout 5 mksh", "resume"),
        ("echo 'delivery-loop resume' |\nsh", "resume"),
        ("cd /tmp\nbash <<<'delivery-loop resume'", "resume"),
        ("cd /tmp\neval 'delivery-loop resume'", "resume"),
        ("bash <<'EOF'\ndelivery-loop resume\nEOF", "resume"),
        ("cat <<'EOF' | bash\ndelivery-loop resume\nEOF", "resume"),
    ],
)
def test_resume_or_abort_is_found_behind_an_abbreviation_eval_or_stdin(command: str, sub) -> None:
    # Value: protects=the early refusal of an agent's own resume; fails_when=an
    # abbreviated option, eval, a here-string or a pipe into a shell hides the call;
    # why_new=verification review of the plugin packaging; seam=none
    assert ri.human_only_command(command) == sub


def test_a_long_shell_line_without_the_cli_is_not_lexed(monkeypatch) -> None:
    # Value: protects=every shell call on the machine stays cheap; fails_when=a 1 MB
    # heredoc is lexed on the no-run path; why_new=review performance finding; seam=none
    monkeypatch.setattr(ri, "_segments", lambda command: pytest.fail("lexed"))
    assert ri.cli_calls("cat <<'EOF'\n" + "x" * 1_000_000 + "\nEOF") == []
    assert ri.git_calls("echo " + "y" * 1_000_000) == []
    assert ri.human_only_command("z" * 1_000_000) is None


def test_a_line_that_repeats_the_cli_name_is_checked_in_linear_time() -> None:
    # Value: protects=the guard answers every shell call well inside the hook timeout;
    # fails_when=the resume/abort check rescans the line from each mention of the name;
    # why_new=performance review measured 2.9 s at 48 KB; seam=none
    import time

    line = "echo " + "delivery-loop x " * 3000
    started = time.perf_counter()
    assert ri.human_only_command(line) is None
    assert ri.human_only_command(line + "; delivery-loop resume") == "resume"
    assert time.perf_counter() - started < 1.5  # was 2.9 s when quadratic
    for nested in (
        "eval " + "delivery-loop x " * 3000,
        "bash " + "'<<<delivery-loop x' " * 3000,
        "printf '" + "delivery-loop x " * 3000 + "' | sh",
        "echo 'delivery-loop status'" + " | sh" * 3000,
    ):
        started = time.perf_counter()
        assert ri.human_only_command(nested) is None
        assert time.perf_counter() - started < 1.5


def test_a_path_that_only_names_the_project_is_not_lexed(monkeypatch) -> None:
    # Value: protects=shell calls inside a checkout named delivery-loop stay cheap;
    # fails_when=any mention of the name lexes the whole line; why_new=performance review;
    # seam=none
    monkeypatch.setattr(ri, "_segments", lambda command: pytest.fail("lexed"))
    heredoc = "cat <<'EOF'\n" + "x" * 100_000 + "\nEOF"
    assert not ri.starts_run("cd /src/delivery-loop-90cd961a && " + heredoc)
    assert not ri.starts_run("cd /src/delivery-loop/ && " + heredoc)
    # The CLI's module is named by its dotted name or its path, not by "cli".
    assert ri.human_only_command("cat <<'EOF'\nclient click\nEOF", boot._CLI_MODULE) is None


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "docs: explain delivery-loop resume"',
        "delivery-loop start fix abort handling",
        "echo delivery-loop status --session abort-1",
        "grep -rn 'delivery-loop resume' README.md",
        "printf 'delivery-loop resume' | grep resume",
        'git commit -m "delivery-loop resume fix" && bash scripts/x.sh',
        'bash scripts/x.sh "run delivery-loop resume to continue"',
        'echo "run delivery-loop resume to continue" | bash scripts/notify.sh',
        # Regression: ISSUE-001 - a fixed `sh -c` script was taken to run its stdin
        # Found by /qa on 2026-10-01
        # Report: .gstack/qa-reports/qa-report-delivery-loop-guard-2026-10-01.md
        "grep -rn 'delivery-loop resume' README.md | sh -c 'wc -l'",
        "echo 'next: run delivery-loop resume in a terminal'\ncurl -fsSL https://x/i.sh | bash",
        "grep -c bash <<< 'see delivery-loop resume'",
        "echo 'see delivery-loop resume' | grep -w sh",
        "bash scripts/x.sh 'a <<< delivery-loop resume'",
        "printf 'delivery-loop resume' || sh",
    ],
)
def test_a_mention_of_resume_is_not_a_resume(command: str) -> None:
    # Value: protects=every session on the machine can commit, grep and start tasks that
    # mention resume or abort; fails_when=the refusal reads raw text, not the CLI's own
    # subcommand; why_new=adversarial review; seam=none
    assert ri.human_only_command(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "delivery-loop --session abc start CRB-1",
        "delivery-loop --sess abc start CRB-1",
        "delivery-loop --s abc start CRB-1",
        "printf 'start X' | delivery-loop --args-stdin",
    ],
)
def test_a_start_past_options_or_on_stdin_is_seen(command: str) -> None:
    # Value: protects=the agent's start binds to the agent's session; fails_when=--session
    # before start or --args-stdin hides the start from the intent; why_new=review; seam=none
    assert ri.starts_run(command)


def test_an_unbalanced_quote_does_not_raise() -> None:
    assert ri.cli_calls("delivery-loop abort 'oops") == [["abort", "'oops"]]


@pytest.mark.parametrize(
    ("tool", "tool_input", "sub"),
    [
        ("Bash", {"command": "delivery-loop resume"}, "resume"),
        ("Bash", {"command": "true && delivery-loop abort"}, "abort"),
        ("Bash", {"command": "delivery-loop status"}, None),
        ("Skill", {"skill": "delivery-loop:pipeline", "args": "resume"}, "resume"),
        ("Skill", {"skill": "/delivery-loop:pipeline", "args": "abort now"}, "abort"),
        ("Skill", {"skill": "delivery-loop:pipeline", "args": "status"}, None),
        ("Skill", {"skill": "other:pipeline", "args": "resume"}, None),
        ("Edit", {"file_path": "x"}, None),
        ("Bash", "not a dict", None),
    ],
)
def test_resume_and_abort_are_recognised_from_any_tool(tool, tool_input, sub) -> None:
    # Value: protects=the agent cannot clear its own pause; fails_when=one route is missed;
    # why_new=the spike showed the Skill tool runs the slash command; seam=none
    assert boot.human_only_call(tool, tool_input) == sub


def _run_dir(tmp_path: Path, status: str = "running") -> Path:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = {"status": status, "revision": 4, "history": [], "session_id": "s1"}
    (run_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return run_dir


def test_abort_fails_the_run_and_drops_its_index(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    ri.write_entry("k", str(run_dir), str(tmp_path), "s1")
    message = ri.abort_run("k", ri.read_entry(ri.index_path("k")), "person")
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["ended_reason"] == "aborted"
    assert state["revision"] == 5
    assert state["history"][-1]["event"] == "aborted"
    assert ri.read_entry(ri.index_path("k")) is None
    assert ri.read_entry(ri.session_path("s1")) is None
    assert "Aborted" in message
    assert ri.read_events(str(run_dir), 1)[0]["decision"] == "abort"


def test_abort_leaves_a_finished_run_alone_but_drops_its_index(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path, "done")
    ri.write_entry("k", str(run_dir), str(tmp_path), None)
    ri.abort_run("k", ri.read_entry(ri.index_path("k")), "person")
    assert json.loads((run_dir / "state.json").read_text())["status"] == "done"
    assert ri.read_entry(ri.index_path("k")) is None


def test_the_lock_times_out_while_another_holder_keeps_it(tmp_path: Path) -> None:
    import fcntl

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    fd = os.open(run_dir / ri.LOCK_FILE, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(ri.LockTimeout, match="Retry"), ri.locked(str(run_dir)):
            pass
    finally:
        os.close(fd)
    with ri.locked(str(run_dir)):
        pass


def test_the_event_log_rotates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ri, "EVENTS_MAX_BYTES", 50)
    for i in range(5):
        ri.append_event(str(tmp_path), {"n": i})
    assert (tmp_path / "events.jsonl.1").exists()
    assert ri.read_events(str(tmp_path), 10)[-1]["n"] == 4


def _old(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "version_info", (3, 9, 0, "final", 0))


def test_old_python_passes_when_no_run_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _old(monkeypatch)
    assert ri.old_python()
    payload = {"cwd": str(tmp_path), "tool_name": "Edit", "tool_input": {"file_path": "x"}}
    assert boot.old_python_hook("guard", payload) is None
    assert boot.old_python_hook("stop", {"cwd": str(tmp_path)}) is None


def test_old_python_denies_edits_and_blocks_once_during_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Value: protects=a run is never left unguarded because python3 changed under it;
    # fails_when=the old path fails open with a run active; why_new=old-python path; seam=none
    (tmp_path / ".git").mkdir()
    ri.write_entry(ri.worktree_key(str(tmp_path)), "/runs/a", str(tmp_path), "s1")
    _old(monkeypatch)
    edit = {"cwd": str(tmp_path), "tool_name": "Write", "tool_input": {"file_path": "a.py"}}
    assert "needs python3 >= 3.11, found 3.9" in boot.old_python_hook("guard", edit)["deny"]
    assert "delivery-loop abort" in boot.old_python_hook("guard", edit)["deny"]
    shell = {"cwd": str(tmp_path), "tool_name": "Bash", "tool_input": {"command": "ls"}}
    assert boot.old_python_hook("guard", shell) is None
    # Without the full guard, nothing publishes either.
    push = {**shell, "tool_input": {"command": "git push --force origin main"}}
    assert "deny" in boot.old_python_hook("guard", push)
    mcp = {"cwd": str(tmp_path), "tool_name": "mcp__github__push_files", "tool_input": {}}
    assert "deny" in boot.old_python_hook("guard", mcp)
    stop = {"cwd": str(tmp_path), "session_id": "s1"}
    assert "block" in boot.old_python_hook("stop", stop)
    assert boot.old_python_hook("stop", {**stop, "stop_hook_active": True}) is None
    assert boot.old_python_hook("stop", {**stop, "session_id": "other"}) is None


def test_old_python_leaves_a_paused_run_waiting(tmp_path: Path, monkeypatch) -> None:
    # Value: protects=a paused run waits for its person instead of re-prompting the agent
    # every turn; fails_when=the old Stop path blocks without reading the status;
    # why_new=adversarial review; seam=none
    (tmp_path / ".git").mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "state.json").write_text('{"status": "awaiting_human"}', encoding="utf-8")
    ri.write_entry(ri.worktree_key(str(tmp_path)), str(run_dir), str(tmp_path), "s1")
    _old(monkeypatch)
    assert boot.old_python_hook("stop", {"cwd": str(tmp_path), "session_id": "s1"}) is None


def test_old_python_still_denies_an_agent_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    _old(monkeypatch)
    call = {"tool_name": "Bash", "tool_input": {"command": "delivery-loop resume"}}
    assert "only a person" in boot.old_python_hook("guard", call)["deny"]
    call = {"tool_name": "Bash", "tool_input": {"command": 'eval "delivery-loop resume"'}}
    assert "only a person" in boot.old_python_hook("guard", call)["deny"]


def test_old_python_cli_can_abort_and_show_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".git").mkdir()
    run_dir = _run_dir(tmp_path)
    ri.write_entry(ri.worktree_key(str(tmp_path)), str(run_dir), str(tmp_path), "s1")
    monkeypatch.chdir(tmp_path)
    _old(monkeypatch)
    assert boot.old_python_cli(["start", "x"]) == 2
    assert boot.old_python_cli(["status"]) == 0
    assert "Status: running" in capsys.readouterr().out
    assert boot.old_python_cli(["--session", "s1", "abort"]) == 2  # not a terminal
    assert json.loads((run_dir / "state.json").read_text())["status"] == "running"
    monkeypatch.setattr(ri, "in_terminal", lambda: True)
    assert boot.old_python_cli(["--session", "s1", "abort"]) == 0
    assert json.loads((run_dir / "state.json").read_text())["status"] == "failed"
