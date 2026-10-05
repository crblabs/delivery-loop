#!/usr/bin/env python3
"""Run the delivery loop for real, in a throwaway repository, and journal the result.

This is not part of the test suite or of CI. A person runs it to check that the
loop works end to end with a real Claude Code session:

    python3 e2e/run.py                       every scenario
    python3 e2e/run.py background_wait       one scenario
    python3 e2e/run.py --journal             the last results

Each scenario builds a git repository with its own short stage list, starts an
interactive ``claude`` in a detached tmux session with this working tree as the
plugin, and types one prompt that starts a run. The runner then watches the
run's state file until the run ends, and checks the loop's own records: the
state, the events log and the session transcript. The runner waits; the agent
never does.

Every result is one line in ``journal.jsonl``, and every run keeps its files
(state, events, transcript, last screen) in its own directory, both under
``~/.delivery-loop/e2e`` unless ``$DELIVERY_LOOP_E2E_HOME`` moves them.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
E2E_HOME = Path(os.environ.get("DELIVERY_LOOP_E2E_HOME", "~/.delivery-loop/e2e")).expanduser()
JOURNAL = E2E_HOME / "journal.jsonl"
DEFAULT_MODEL = "claude-opus-5-5"
# The installed copy of this plugin would run every hook a second time.
SETTINGS = {"enabledPlugins": {"delivery-loop@crblabs": False}}
DONE = "<promise>STAGE DONE</promise>"
# A foreground wait in a shell command: what the loop must never need.
SHELL_WAIT_RE = re.compile(r"(^|[;&|(\s])(sleep|wait|until|watch)\s|\btail\s+-f\b")


@dataclass
class Scenario:
    """``stages`` maps a stage name to its prompt, or to (prompt, extra loop.toml keys).

    ``replies`` are what a person types: each waits for the run to reach a status,
    then types the text.
    """

    name: str
    goal: str
    stages: dict[str, str | tuple[str, dict]]
    check: Callable[[Result], None]
    replies: list[tuple[str, str]] = field(default_factory=list)
    timeout_s: int = 900


@dataclass
class Result:
    sandbox: Path
    state: dict
    events: list[dict]
    transcript: list[dict]
    failures: list[str] = field(default_factory=list)

    def expect(self, ok: bool, message: str) -> None:
        if not ok:
            self.failures.append(message)

    def committed(self, path: str) -> str | None:
        shown = _git(self.sandbox, "show", f"HEAD:{path}", check=False)
        return shown.stdout if shown.returncode == 0 else None

    def _blocks(self):
        for entry in self.transcript:
            content = (entry.get("message") or {}).get("content")
            yield from (
                (b for b in content if isinstance(b, dict)) if isinstance(content, list) else ()
            )

    def shell_commands(self) -> list[str]:
        """The foreground shell commands that ran; a denied one never did."""
        failed = {
            b.get("tool_use_id")
            for b in self._blocks()
            if b.get("type") == "tool_result" and b.get("is_error")
        }
        commands = []
        for block in self._blocks():
            if block.get("type") != "tool_use" or block.get("name") != "Bash":
                continue
            tool_input = block.get("input") or {}
            if not tool_input.get("run_in_background") and block.get("id") not in failed:
                commands.append(str(tool_input.get("command", "")))
        return commands

    def decisions(self, hook: str) -> list[str]:
        return [str(e.get("decision")) for e in self.events if e.get("hook") == hook]


def _check_common(r: Result) -> None:
    r.expect(r.state.get("status") == "done", f"run ended as {r.state.get('status')!r}")
    waits = [c for c in r.shell_commands() if SHELL_WAIT_RE.search(c)]
    r.expect(not waits, f"the agent waited in a foreground shell: {waits[:2]}")
    errors = [e for e in r.events if e.get("decision") == "error"]
    r.expect(not errors, f"a hook failed: {errors[:2]}")


def _check_background_wait(r: Result) -> None:
    _check_common(r)
    decisions = r.decisions("stop")
    r.expect("wait" in decisions, f"no turn ended as a wait; stop decisions: {decisions}")
    r.expect("READY" in (r.committed("wait.txt") or ""), "wait.txt with READY is not committed")
    r.expect(r.committed("after.txt") is not None, "the second stage did not run")


def _check_happy_path(r: Result) -> None:
    _check_common(r)
    for name in ("one.txt", "two.txt", "three.txt"):
        r.expect(r.committed(name) is not None, f"{name} is not committed")
    decisions = r.decisions("stop")
    r.expect(decisions.count("block") >= 2, f"the run did not advance twice: {decisions}")
    r.expect("wait" not in decisions, f"a turn waited with nothing in the background: {decisions}")
    r.expect(not r.state.get("attempts", {}).get("three"), "the last stage needed a retry")


def _check_guard_sleep(r: Result) -> None:
    _check_common(r)
    r.expect("deny" in r.decisions("guard"), f"the guard denied nothing: {r.decisions('guard')}")
    r.expect(r.committed("denied.txt") is not None, "denied.txt is not committed")


def _check_guarded_edit(r: Result) -> None:
    _check_common(r)
    r.expect("deny" in r.decisions("guard"), f"the guard denied nothing: {r.decisions('guard')}")
    r.expect("e2e edit" not in (r.committed("loop.toml") or ""), "the loop.toml edit landed")
    r.expect(r.committed("denied.txt") is not None, "denied.txt is not committed")


def _check_pause_resume(r: Result) -> None:
    _check_common(r)
    decisions = r.decisions("stop")
    r.expect("pause" in decisions, f"the run never paused: {decisions}")
    r.expect("BETA" in (r.committed("choice.txt") or ""), "choice.txt does not hold BETA")


SCENARIOS = {
    s.name: s
    for s in [
        Scenario(
            name="background_wait",
            goal="CRB-28: a turn beside a background agent waits; its report resumes the stage",
            stages={
                "wait": (
                    "Start exactly one subagent with the Agent tool, in the background. Its "
                    "prompt: 'Reply with the word READY and nothing else.' Then end your turn "
                    "at once, with no token: do not poll, sleep, or read its output file. When "
                    "its result arrives, write the reply to wait.txt, commit it, and end that "
                    f"turn with the line {DONE}"
                ),
                "after": (
                    f"Write the word AFTER to after.txt, commit it, and end with the line {DONE}"
                ),
            },
            check=_check_background_wait,
        ),
        Scenario(
            name="happy_path",
            goal="three stages advance in order, one of them over a committed tree",
            stages={
                "one": f"Write ONE to one.txt, commit it, and end with the line {DONE}",
                "two": (
                    f"Write TWO to two.txt, commit it, and end with the line {DONE}",
                    {"clean_tree": True},
                ),
                "three": f"Write THREE to three.txt, commit it, and end with the line {DONE}",
            },
            check=_check_happy_path,
        ),
        Scenario(
            name="guard_sleep",
            goal="the guard denies a foreground shell wait, and the stage still finishes",
            stages={
                "sleepy": (
                    "Run the shell command `sleep 3` in the foreground, not in the background. "
                    "If the tool refuses it, do not retry or work around it: write the first "
                    "line of the refusal to denied.txt, commit it, and end with the line "
                    f"{DONE}"
                ),
            },
            check=_check_guard_sleep,
        ),
        Scenario(
            name="guarded_edit",
            goal="the guard denies an edit to loop.toml, an enforcement file",
            stages={
                "edit": (
                    "Use the Edit tool to append the line `# e2e edit` to loop.toml. If the "
                    "tool refuses it, do not retry or work around it: write the first line of "
                    f"the refusal to denied.txt, commit it, and end with the line {DONE}"
                ),
            },
            check=_check_guarded_edit,
        ),
        Scenario(
            name="pause_resume",
            goal="a NEEDS HUMAN card pauses the run, and a typed resume with the answer resumes it",
            stages={
                "ask": (
                    "Ask the person which word to write. End this turn with this decision card "
                    "and then the line <promise>NEEDS HUMAN</promise>:\n\n"
                    "ASK   Which word should I write to choice.txt?\n"
                    "REC   1. ALPHA\nALT   2. BETA\nCOST  none\nREPLY 1 | 2\n\n"
                    "After the person answers, write the chosen word to choice.txt, commit it, "
                    f"and end with the line {DONE}"
                ),
            },
            replies=[("awaiting_human", "/delivery-loop:pipeline resume My answer: 2 (BETA)")],
            check=_check_pause_resume,
        ),
    ]
}


def _run(args: list[str], cwd: Path | None = None, check: bool = True, **kw):
    return subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True, **kw)


def _git(cwd: Path, *args: str, check: bool = True):
    return _run(["git", *args], cwd=cwd, check=check)


def _tmux(*args: str, check: bool = True):
    return _run(["tmux", *args], check=check)


def build_sandbox(scenario: Scenario, run_dir: Path) -> Path:
    """A repository with an origin, a loop.toml naming the stages, and their prompts."""
    origin, repo = run_dir / "origin.git", run_dir / "e2e-sandbox"
    _git(run_dir, "init", "-q", "--bare", str(origin))
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "e2e@delivery-loop.invalid")
    _git(repo, "config", "user.name", "delivery-loop e2e")
    (repo / "README.md").write_text(f"# e2e sandbox: {scenario.name}\n", encoding="utf-8")
    prompts = repo / ".claude/skills/pipeline/stages"
    prompts.mkdir(parents=True)
    toml = ""
    for name, spec in scenario.stages.items():
        text, extra = (spec, {}) if isinstance(spec, str) else spec
        toml += f'[[stages]]\nname = "{name}"\n'
        toml += "".join(f"{key} = {json.dumps(value)}\n" for key, value in extra.items()) + "\n"
        (prompts / f"{name}.md").write_text(text + "\n", encoding="utf-8")
    (repo / "loop.toml").write_text(toml, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "e2e sandbox")
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo.resolve()


def _screen(session: str) -> str:
    return _tmux("capture-pane", "-p", "-t", session, check=False).stdout


def _type(session: str, text: str) -> None:
    _tmux("send-keys", "-t", session, "-l", text)
    time.sleep(0.5)
    _tmux("send-keys", "-t", session, "Enter")


def start_session(repo: Path, home: Path, model: str, session_id: str, session: str) -> None:
    """An interactive claude in tmux, past its start dialogs, ready for a prompt."""
    command = shlex.join(
        [
            "claude",
            "--session-id",
            session_id,
            "--model",
            model,
            "--plugin-dir",
            str(ROOT),
            "--settings",
            json.dumps(SETTINGS),
            "--dangerously-skip-permissions",
        ]
    )
    _tmux(
        "new-session", "-d", "-s", session, "-x", "220", "-y", "60", "-c", str(repo),
        "-e", f"DELIVERY_LOOP_HOME={home}", command,
    )  # fmt: skip
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        screen = _screen(session)
        # Both dialogs select their "no, exit" choice first: move down, then confirm.
        if "Yes, I trust this folder" in screen or "Yes, I accept" in screen:
            _tmux("send-keys", "-t", session, "Down")
            time.sleep(0.5)
            _tmux("send-keys", "-t", session, "Enter")
            time.sleep(2)
        elif "bypass permissions on" in screen:
            return
        time.sleep(1)
    raise RuntimeError("claude did not reach its prompt within 60 s:\n" + _screen(session))


def _state_path(home: Path) -> Path | None:
    found = sorted(home.glob("runs/*/*/state.json"))
    return found[0] if found else None


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _read_jsonl(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def watch(home: Path, session: str, deadline: float, until: str | None = None) -> tuple[dict, str]:
    """Wait for the run to reach ``until``, or to leave ``running``: (state, why it ended)."""
    timeout_s = round(deadline - time.monotonic())
    state: dict = {}
    while time.monotonic() < deadline:
        path = _state_path(home)
        state = _read_json(path) if path else {}
        status = state.get("status")
        if status not in (None, "running") or (until is not None and status == until):
            return state, str(status)
        if _tmux("has-session", "-t", session, check=False).returncode != 0:
            return state, "claude exited"
        time.sleep(3)
    return state, f"timed out after {timeout_s} s"


def _version() -> str:
    return _run(["claude", "--version"], check=False).stdout.strip()


def _source() -> dict:
    sha = _git(ROOT, "rev-parse", "--short", "HEAD").stdout.strip()
    dirty = bool(_git(ROOT, "status", "--porcelain").stdout.strip())
    branch = _git(ROOT, "branch", "--show-current").stdout.strip()
    return {"sha": sha, "dirty": dirty, "branch": branch}


def run_scenario(scenario: Scenario, model: str) -> dict:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = E2E_HOME / "runs" / f"{stamp}-{scenario.name}"
    run_dir.mkdir(parents=True)
    home, session_id = run_dir / "home", str(uuid.uuid4())
    session = f"dl-e2e-{scenario.name}-{stamp}"
    started = time.monotonic()
    record = {
        "at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "scenario": scenario.name,
        "source": _source(),
        "claude": _version(),
        "model": model,
        "dir": str(run_dir),
    }
    try:
        repo = build_sandbox(scenario, run_dir)
        start_session(repo, home, model, session_id, session)
        _type(
            session,
            f'Run `delivery-loop start "e2e {scenario.name}"` in the shell, then do the '
            "stage it prints, following the delivery-loop pipeline skill.",
        )
        deadline = time.monotonic() + scenario.timeout_s
        for status, text in scenario.replies:
            state, ended = watch(home, session, deadline, until=status)
            if state.get("status") != status:
                break
            _type(session, text)
            # The resume flips the status at once; wait for the next change from there.
            time.sleep(5)
        state, ended = watch(home, session, deadline)
        state_path = _state_path(home)
        events = _read_jsonl(state_path.parent / "events.jsonl" if state_path else None)
        transcripts = list(Path("~/.claude/projects").expanduser().glob(f"*/{session_id}.jsonl"))
        transcript = transcripts[0] if transcripts else None
        result = Result(repo, state, events, _read_jsonl(transcript))
        scenario.check(result)
        if state.get("status") != "done":
            result.failures.insert(0, f"watch ended: {ended}")
        record.update(
            ok=not result.failures,
            failures=result.failures,
            ended=ended,
            stage=state.get("current_stage"),
            pending_question=state.get("pending_question"),
        )
        for path in (state_path, state_path and state_path.parent / "events.jsonl", transcript):
            if path and Path(path).exists():
                shutil.copy(path, run_dir / Path(path).name)
    except Exception as exc:  # noqa: BLE001 - a broken run is a journaled failure
        record.update(ok=False, failures=[f"runner error: {exc!r}"], ended="error")
    finally:
        (run_dir / "screen.txt").write_text(_screen(session), encoding="utf-8")
        _tmux("kill-session", "-t", session, check=False)
    record["seconds"] = round(time.monotonic() - started)
    with JOURNAL.open("a", encoding="utf-8") as journal:
        journal.write(json.dumps(record) + "\n")
    return record


def show_journal(count: int) -> None:
    for row in _read_jsonl(JOURNAL)[-count:]:
        source = row.get("source") or {}
        mark = "PASS" if row.get("ok") else "FAIL"
        print(
            f"{row.get('at')}  {mark}  {row.get('scenario')}  {row.get('seconds')}s  "
            f"{source.get('branch')}@{source.get('sha')}{'+dirty' if source.get('dirty') else ''}  "
            f"{row.get('claude')}"
        )
        for failure in row.get("failures") or []:
            print(f"      - {failure}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("scenarios", nargs="*", help=f"default: all ({', '.join(SCENARIOS)})")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--journal", nargs="?", const=20, type=int, metavar="N")
    args = parser.parse_args(argv)
    E2E_HOME.mkdir(parents=True, exist_ok=True)
    if args.journal:
        show_journal(args.journal)
        return 0
    unknown = [name for name in args.scenarios if name not in SCENARIOS]
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(unknown)}")
    if not shutil.which("tmux") or not shutil.which("claude"):
        parser.error("needs tmux and claude on PATH")
    records = [run_scenario(SCENARIOS[name], args.model) for name in args.scenarios or SCENARIOS]
    show_journal(len(records))
    return 0 if all(r["ok"] for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
