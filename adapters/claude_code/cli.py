"""``delivery-loop``: start, inspect, resume and end a run, and check a machine.

The plugin ships this as ``bin/delivery-loop``, which Claude Code puts on the
agent's PATH, and the slash command ``/delivery-loop:pipeline`` runs it with the
session id and its arguments on stdin (``--args-stdin``), so a task title may
hold any character. ``resume`` and ``abort`` clear or end a pause, so only a
person runs them: here, in a terminal, or by typing the slash command, which the
plugin's prompt hook applies.

    delivery-loop start <task> [--config FILE]   start a run in this worktree
    delivery-loop status                          the run's stage, pause and events
    delivery-loop resume                          continue a paused run (a person)
    delivery-loop abort                           end the run (a person)
    delivery-loop doctor                          check python, git, state, skills

Exit codes: ``0`` done, ``1`` a check failed (``doctor``), ``2`` refused or
unusable input, with the reason on stderr.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from adapters.claude_code import bootstrap as boot
from core import run_index as ri
from core import run_state as rs
from core.config import HARNESS_HOME, HARNESS_HOME_ENV, ConfigError, LoopConfig, load_config

PLUGIN_BIN = rs.PLUGIN_ROOT / "bin" / ri.CLI_NAME
_CONFIG_TAIL_RE = re.compile(r"\s--config(?:=|\s+)(\S+)\s*$")
_OLD_COMMANDS = ("/pipeline resume", "/pipeline abort")


def _parser() -> argparse.ArgumentParser:
    # No abbreviations: ``--sess`` or ``--args`` would hide a subcommand or a
    # session from the guard, which reads the words as typed.
    parser = argparse.ArgumentParser(
        prog=ri.CLI_NAME,
        description="Drive the delivery loop in this worktree.",
        allow_abbrev=False,
    )
    parser.add_argument("--session", default=None, help="the Claude Code session id")
    parser.add_argument(
        "--args-stdin", action="store_true", help="read the subcommand and its words from stdin"
    )
    sub = parser.add_subparsers(dest="command")
    start = sub.add_parser("start", help="start a run in this worktree", allow_abbrev=False)
    start.add_argument("task", nargs="*", help="the task: an issue id or a short title")
    start.add_argument("--config", default=None, help="a loop.toml to run under")
    sub.add_parser("status", help="show the run in this worktree")
    sub.add_parser("resume", help="continue a paused run (a person runs this)")
    sub.add_parser("abort", help="end the run in this worktree (a person runs this)")
    sub.add_parser("doctor", help="check this machine and worktree")
    return parser


def _fail(message: str) -> int:
    print(f"delivery-loop: {message}", file=sys.stderr)
    return 2


def _session(args: argparse.Namespace) -> str | None:
    value = args.session or os.environ.get(boot.SESSION_ENV)
    return value if value else None


def _find(args: argparse.Namespace) -> rs.Run | None:
    return rs.find(os.getcwd(), None, _session(args))


def _load(path: str | None, worktree: Path | None) -> LoopConfig:
    if path is not None:
        named = Path(path) / "loop.toml" if Path(path).is_dir() else Path(path)
        if not named.is_file():
            raise ConfigError(f"--config names no file: {named}")
        return load_config(named)
    return load_config(worktree, implicit=True) if worktree else load_config(None)


def cmd_start(args: argparse.Namespace) -> int:
    task = " ".join(args.task).strip()
    if not task:
        return _fail(
            "start needs a task. Cause: no task was given. Fix: "
            f"/{boot.COMMAND_NAME} start CRB-30 (or delivery-loop start CRB-30)."
        )
    root = ri.worktree_root(os.getcwd())
    try:
        config = _load(args.config, Path(root) if root else None)
    except ConfigError as exc:
        return _fail(f"CONFIG_INVALID: {exc}")
    # The intent the guard recorded from the harness's own payload outranks a
    # session given as an argument, which any caller can type.
    intent = ri.take_intent(ri.worktree_key(root)) if root is not None else None
    if ri.in_terminal():
        # A person's own start: an intent left by an agent call that was then
        # declined or never ran is not theirs.
        intent = None
    session = intent or _session(args)
    # The slash command names its session through ${CLAUDE_SESSION_ID}; if a
    # harness leaves that empty, the run still starts and binds at the first turn
    # end, as before.
    if session is None and not ri.in_terminal() and not args.args_stdin:
        # Unbound, the run would go to whichever session in this worktree ends a
        # turn first, not necessarily the one that started it.
        return _fail(
            "start could not tell which Claude Code session runs it. Cause: it was run "
            "from a script or a nested shell, not the slash command or a terminal. Fix: "
            f"/{boot.COMMAND_NAME} start <task>, or delivery-loop start <task> in a terminal."
        )
    try:
        _, text = rs.start(Path(os.getcwd()), task, config, session)
    except (rs.RunError, ri.LockTimeout) as exc:
        return _fail(str(exc))
    print(text)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    try:
        run = _find(args)
    except rs.RunError as exc:
        return _fail(str(exc))
    if run is None:
        print("No active delivery-loop run in this worktree. Start one: delivery-loop start <task>")
        return 0
    print(rs.status_text(run))
    return 0


def _person_only(args: argparse.Namespace) -> int | None:
    """Refuse resume and abort unless a person is at a terminal.

    From the slash command they are not refused but deferred: the plugin's
    prompt hook applies the command a person typed, right after this runs.
    """
    if args.args_stdin:
        print(
            f"{args.command} is applied by the plugin's prompt hook for the command a person "
            f"typed. Its result follows as a `delivery-loop {args.command}:` note; with no "
            "note, nothing changed: run delivery-loop status."
        )
        return 0
    if not ri.in_terminal():
        return _fail(
            f"{args.command} is for a person. Cause: it clears or ends a pause. Fix: type "
            f"/{boot.COMMAND_NAME} {args.command} in Claude Code, or run it in a terminal."
        )
    return None


def cmd_resume(args: argparse.Namespace) -> int:
    refused = _person_only(args)
    if refused is not None:
        return refused
    try:
        run = _find(args)
        if run is None:
            return _fail("no active run in this worktree. Fix: delivery-loop status.")
        # A terminal has no harness session to bind, so the run keeps its own. The
        # record says where the resume came from: a pty is not proof of a person.
        print(rs.resume(run, "terminal"))
    except (rs.RunError, ri.LockTimeout) as exc:
        return _fail(str(exc))
    return 0


def cmd_abort(args: argparse.Namespace) -> int:
    refused = _person_only(args)
    if refused is not None:
        return refused
    found = ri.find_entry(os.getcwd(), None, _session(args))
    if found is None:
        print("No active delivery-loop run in this worktree.")
        return 0
    try:
        print(ri.abort_run(*found, "terminal"))
    except ri.LockTimeout as exc:
        return _fail(str(exc))
    return 0


# --- doctor --------------------------------------------------------------------


def _skill_dirs(worktree: Path | None) -> list[Path]:
    home = Path(os.environ.get(HARNESS_HOME_ENV) or HARNESS_HOME).expanduser()
    places = [home / "skills", home / "commands"]
    if worktree is not None:
        local = worktree / Path(HARNESS_HOME).name
        places += [local / "skills", local / "commands"]
    cache = home / "plugins" / "cache"
    if cache.is_dir():
        places += sorted(cache.glob("*/*/*/skills")) + sorted(cache.glob("*/*/*/commands"))
    return places


def _command_found(command: str, places: list[Path]) -> bool:
    name = command.lstrip("/").split(":")[-1].split()[0] if command.strip() else ""
    if not name:
        return True
    for place in places:
        if (place / name / "SKILL.md").is_file() or (place / f"{name}.md").is_file():
            return True
    return False


def _hook_python() -> tuple[str, tuple[int, ...] | None]:
    """The ``python3`` a hook runs with, the first on PATH as Claude Code finds it,
    and its version; ``("missing", None)`` when there is none."""
    found = shutil.which("python3")
    if found is None:
        return "missing", None
    try:
        out = subprocess.run(
            [found, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
        return found, tuple(int(part) for part in out.split("."))
    except (OSError, subprocess.SubprocessError, ValueError):
        return found, None


def doctor_checks(cwd: Path) -> list[tuple[str, str, str]]:
    """Each check as (level, name, detail); level is ok, warn or fail."""
    checks: list[tuple[str, str, str]] = []
    floor = "{}.{}".format(*ri.FLOOR)
    version = "{}.{}".format(*sys.version_info[:2])
    checks.append(("ok", "python", f"{sys.executable} {version} (floor {floor})"))
    path, hook_version = _hook_python()
    hook_ok = hook_version is not None and hook_version >= ri.FLOOR
    shown = "{}.{}".format(*hook_version) if hook_version else "unknown version"
    detail = f"{path} {shown}" if path != "missing" else "no python3 on PATH"
    checks.append(("ok" if hook_ok else "fail", "hook python3", f"{detail} (floor {floor})"))
    try:
        import fcntl  # noqa: F401

        checks.append(("ok", "platform", sys.platform))
    except ImportError:
        checks.append(("fail", "platform", f"{sys.platform} is not supported (no fcntl)"))
    root = ri.worktree_root(str(cwd))
    worktree = Path(root) if root else None
    if worktree is None:
        checks.append(("fail", "git worktree", f"{cwd} is not in one; git init or cd into one"))
    else:
        checks.append(("ok", "git worktree", str(worktree)))
        checks.append(("ok", "repository", rs.detect_repo(worktree)))
    home = Path(ri.state_home())
    try:
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=home):
            pass
        checks.append(("ok", "state home", str(home)))
    except OSError as exc:
        checks.append(("fail", "state home", f"{home} is not writable: {exc}"))
    try:
        config = _load(None, worktree)
        checks.append(("ok", "config", "loads"))
    except ConfigError as exc:
        checks.append(("fail", "config", str(exc)))
        config = None
    if config is not None:
        for old in _OLD_COMMANDS:
            if old in (config.resume_command, config.abort_command):
                checks.append(
                    ("warn", "commands", f"config still names {old}; use /delivery-loop:pipeline")
                )
        places = _skill_dirs(worktree)
        for stage in config.stages:
            if worktree is not None and rs.resolve_prompt(stage, worktree, config) is None:
                checks.append(("fail", f"stage {stage.name}", f"no prompt file {stage.prompt}"))
            if stage.command and not _command_found(stage.command, places):
                searched = ", ".join(str(p) for p in places[:4])
                checks.append(
                    (
                        "warn",
                        f"stage {stage.name}",
                        f"{stage.command} not found in {searched} or the plugin cache; install "
                        "the skill or declare other stages in loop.toml",
                    )
                )
        if worktree is not None:
            legacy = rs.legacy_install(worktree, config)
            if legacy:
                checks.append(
                    ("fail", "legacy install", "delete " + ", ".join(legacy) + " and their hooks")
                )
    checks.append(("ok", "cli", f"{PLUGIN_BIN}; in a terminal: alias {ri.CLI_NAME}='{PLUGIN_BIN}'"))
    return checks


def cmd_doctor(_args: argparse.Namespace) -> int:
    checks = doctor_checks(Path(os.getcwd()))
    for level, name, detail in checks:
        print(f"{level.upper():4}  {name}: {detail}")
    return 1 if any(level == "fail" for level, _, _ in checks) else 0


COMMANDS = {
    "start": cmd_start,
    "status": cmd_status,
    "resume": cmd_resume,
    "abort": cmd_abort,
    "doctor": cmd_doctor,
}


def _from_stdin(argv: list[str]) -> list[str]:
    """The arguments with stdin's words in place of ``--args-stdin``'s.

    The slash command passes what a person typed through a quoted heredoc, so no
    shell ever reads it. The first word is the subcommand; for ``start`` the rest
    is the task, kept as one string.
    """
    text = sys.stdin.read().strip()
    words = text.split(None, 1)
    if not words:
        return argv
    sub, rest = words[0], (words[1] if len(words) > 1 else "")
    if sub != "start":
        return [*argv, sub, *rest.split()]
    # A trailing --config names a file; the rest of the line is the task as typed.
    match = _CONFIG_TAIL_RE.search(rest)
    config = ["--config", match.group(1)] if match else []
    task = rest[: match.start()] if match else rest
    return [*argv, sub, *([task] if task else []), *config]


def _refuse_unknown_options(parser: argparse.ArgumentParser, argv: list[str]) -> None:
    """Name an unknown global option, such as ``--sess``, before argparse reads
    the word after it as the subcommand and reports that instead."""
    known = set(parser._option_string_actions)
    i = 0
    while i < len(argv) and argv[i].startswith("-") and argv[i] != "--":
        if argv[i].split("=", 1)[0] not in known:
            parser.error(f"unrecognized arguments: {argv[i]}")
        i += 2 if argv[i] == "--session" else 1


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    argv = sys.argv[1:] if argv is None else argv
    if "--args-stdin" in argv:
        argv = _from_stdin(argv)
    _refuse_unknown_options(parser, argv)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    return COMMANDS[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
