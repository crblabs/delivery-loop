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


def test_a_long_shell_line_without_the_cli_is_not_lexed(monkeypatch) -> None:
    # Value: protects=every shell call on the machine stays cheap; fails_when=a 1 MB
    # heredoc is lexed on the no-run path; why_new=review performance finding; seam=none
    monkeypatch.setattr(ri, "segments", lambda command: pytest.fail("lexed"))
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


def test_a_path_that_only_names_the_project_is_not_lexed(monkeypatch) -> None:
    # Value: protects=shell calls inside a checkout named delivery-loop stay cheap;
    # fails_when=any mention of the name lexes the whole line; why_new=performance review;
    # seam=none
    monkeypatch.setattr(ri, "segments", lambda command: pytest.fail("lexed"))
    heredoc = "cat <<'EOF'\n" + "x" * 100_000 + "\nEOF"
    assert not ri.starts_run("cd /src/delivery-loop-90cd961a && " + heredoc)
    assert not ri.starts_run("cd /src/delivery-loop/ && " + heredoc)


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "docs: explain delivery-loop resume"',
        "delivery-loop start fix abort handling",
        "echo delivery-loop status --session abort-1",
        "grep -rn 'delivery-loop resume' README.md",
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


def test_each_unquoted_line_is_its_own_command() -> None:
    # Value: protects=the newline separator that never fired; fails_when=a multi-line script
    # reads as one command again; why_new=CRB-28 Eng #69; seam=none
    assert ri.segments("while ! grep -q x f\ndo\n  sleep 30\ndone") == [
        ["while", "!", "grep", "-q", "x", "f"],
        ["do"],
        ["sleep", "30"],
        ["done"],
    ]
    assert ri.segments('git commit -m "a\nb"') == [["git", "commit", "-m", "a\nb"]]
    assert ri.segments("git push \\\n  --force origin") == [["git", "push", "--force", "origin"]]


def test_a_command_on_a_later_line_is_seen() -> None:
    # Value: protects=the intended differences of the newline split; fails_when=a push or a
    # start on line 2 is hidden again; why_new=CRB-28 Eng #69; seam=none
    assert ri.git_calls("cd x\ngit push --force") == [["push", "--force"]]
    assert ri.starts_run(f"cd x\n{ri.CLI_NAME} start CRB-1")


def test_a_heredoc_body_is_data_not_a_command() -> None:
    # Value: protects=review adversarial: a commit message with a "git push" line is not a
    # push, now that each line is a command; fails_when=_calls lexes heredoc bodies;
    # why_new=review regression; seam=none
    message = "git commit -F - <<EOF\nfix\n\ngit push --force origin main\nEOF\ngit push"
    assert ri.git_calls(message) == [["commit", "-F", "-", "<<EOF"], ["push"]]
    assert not ri.starts_run(f"git commit -F - <<'EOF'\n{ri.CLI_NAME} start CRB-1\nEOF")
    # A command that runs its body is still read whole by the human-only check.
    assert ri.human_only_command(f"bash <<EOF\n{ri.CLI_NAME} abort\nEOF") == "abort"


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ('cat <<< "x"\nnext', 'cat <<< "x"\nnext'),
        ("echo $((1<<2))\nnext", "echo $((1<<2))\nnext"),
        ('echo "<<EOF"\nnext\nEOF', 'echo "<<EOF"\nnext\nEOF'),
        (": # <<X\nnext", ": # <<X\nnext"),
        ("cat <<-'E'\n\tbody\n\tE\nnext", "cat <<-'E'\nnext"),
        ("cat <<A <<B\na\nA\nb\nB\nnext", "cat <<A <<B\nnext"),
        ("cat <<\\EOF\nbody\nEOF\nnext", "cat <<\\EOF\nnext"),
        ("cat <<EOF\nnever closed", "cat <<EOF\nnever closed"),
    ],
)
def test_only_a_real_heredoc_body_is_stripped(text: str, kept: str) -> None:
    assert ri.strip_heredocs(text) == kept


def test_a_backslash_newline_is_text_inside_single_quotes() -> None:
    # Value: protects=the lexer reads words as the shell does; fails_when=a quoted
    # backslash-newline is joined; why_new=review adversarial; seam=none
    assert ri.segments("echo 'a\\\nb'; x") == [["echo", "a\\\nb"], ["x"]]
    assert ri.segments('echo "a\\\nb"') == [["echo", "ab"]]


def test_a_shell_line_is_lexed_once_for_every_rule_that_reads_it() -> None:
    # Value: protects=the guard's cost on a long line read by the gh, wait and push rules;
    # fails_when=each rule lexes it again, or one caller's lists leak into another's;
    # why_new=review performance; seam=lru cache counters
    line = "gh pr view 1 && git push origin x && sleep 1 # " + "y" * 50_000
    ri._lex.cache_clear()
    ri.gh_calls(line)
    ri.git_calls(line)
    first = ri.segments(line)
    first[0].append("mutated")
    assert ri.segments(line)[0][-1] != "mutated"
    assert ri._lex.cache_info().misses == 1


@pytest.mark.parametrize(
    "first",
    [": ${x#<<}", "echo $[1<<2]", "echo $'a\\'b' '<<EOF'", "cat <<EOF"],
)
def test_a_misread_heredoc_cannot_hide_the_next_line(first: str) -> None:
    # Value: protects=review security and adversarial: a "<<" bash does not read as a
    # here-document (an expansion, $[..], ANSI-C quoting, an unclosed body) never hides a
    # push on a later line; fails_when=an unclosed body is dropped; why_new=review; seam=none
    assert ri.git_calls(f"{first}\ngit push --force origin main") == [
        ["push", "--force", "origin", "main"]
    ]


def test_a_megabyte_word_is_lexed_quickly_and_whole(monkeypatch) -> None:
    # Value: protects=the guard answers a call with a megabyte word inside its timeout and
    # still reads every command on the line; fails_when=shlex reads the whole word (40 s)
    # or the word is dropped; why_new=ISSUE-001 and its reverted fix; seam=spy on shlex
    import shlex

    seen: list[int] = []
    real = shlex.shlex
    monkeypatch.setattr(
        ri.shlex, "shlex", lambda text, *a, **k: seen.append(len(text)) or real(text, *a, **k)
    )
    ri._lex.cache_clear()
    body = "wait " * 200_000
    calls = ri.gh_calls(f'gh pr create --body "{body}"\ngh pr merge 3')
    assert calls[0][:3] == ["pr", "create", "--body"] and calls[0][3] == body
    assert calls[1] == ["pr", "merge", "3"]
    assert ri.git_calls("echo " + "a" * 1_000_000 + "\ngit push --force origin main")[-1] == [
        "push",
        "--force",
        "origin",
        "main",
    ]
    assert max(seen) < 10 * ri.LEX_WORD_MAX


@pytest.mark.parametrize(
    "command",
    [
        'claude -p "/delivery-loop:pipeline resume ' + "x " * 35_000 + '"',
        "# don't panic\n" + "echo x\n" * 20_000 + "delivery-loop resume",
        "echo $'a\\'' ; delivery-loop resume ; echo \"" + "a" * 70_000 + '"',
    ],
)
def test_a_long_line_never_hides_a_human_only_command(command: str) -> None:
    # Value: protects=ship review: the three ways the reverted shortening hid a resume
    # (a long quoted prompt, a stray apostrophe, ANSI-C quoting) stay visible; fails_when=a
    # long word is dropped instead of restored; why_new=ship red team; seam=none
    assert ri.human_only_command(command) == "resume"


def test_placeholder_lexing_matches_shlex_word_for_word(monkeypatch) -> None:
    # Value: protects=the placeholder pass reads quoting exactly as shlex does; fails_when=a
    # quote, escape or separator is read differently; why_new=linear lexer; seam=a small
    # LEX_WORD_MAX so every word goes through a placeholder
    import random

    rng = random.Random(28)
    alphabet = ["a", "b", " ", "'", '"', "\\", ";", "|", "&", "\n", "$", "(", "x"]
    checked = 0
    for _ in range(3000):
        command = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24)))
        try:
            expected = ri._shlex_words(command)
        except ValueError:
            continue
        monkeypatch.setattr(ri, "LEX_WORD_MAX", 0)
        swapped = ri._placeholders(command)
        monkeypatch.setattr(ri, "LEX_WORD_MAX", 4096)
        assert swapped is not None, command
        text, table = swapped
        got = [
            ri._PLACEHOLDER_RE.sub(lambda m, table=table: table[int(m.group(1))], w)
            for w in ri._shlex_words(text)
        ]
        assert got == expected, command
        checked += 1
    assert checked > 1000
