"""Where a hook finds the run it belongs to, readable by any python3.

Both hooks fire in every session in every repository on the machine, because the
plugin is installed once per user. Most of those calls belong to no run, so the
first thing a hook does is ask this module whether one exists, and it must
answer in a few file checks: no config, no git, no import of the rest of
``core``.

The answer is the run index under the state home, ``~/.delivery-loop`` unless
``DELIVERY_LOOP_HOME`` moves it:

  * ``index/<worktree key>`` names the run directory of the run active in one
    worktree, and the session bound to it. The key is the first 12 hex digits
    of the sha256 of the worktree's real path, as in ``pipeline_state.run_dir``.
  * ``index/session/<session id>`` names the same run for its bound session, so
    the Stop hook still finds it after the agent changes directory.
  * ``intent/<worktree key>`` holds the session that is about to run
    ``delivery-loop start``, for at most ``INTENT_TTL_S`` seconds.

``start`` writes the entries and ``abort`` and the end of a run remove them. The
state file stays the source of truth: the index is rewritten from it.

The module also holds what must work on an interpreter older than the floor
(``FLOOR``): the run lookup and ``abort``, so a person whose ``python3`` changed
under a run can still end it. Only what the no-run path needs is imported at
module level; a write path imports the rest when it runs. It parses on Python
3.8, the oldest ``python3`` a supported machine ships, and a test holds it to
that. It names no harness: the
harness's old-Python hook path is in its adapter's ``bootstrap`` module.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import shlex
import stat
import sys
import time

FLOOR = (3, 11)
HOME_ENV = "DELIVERY_LOOP_HOME"
DEFAULT_HOME = "~/.delivery-loop"
INDEX_DIR = "index"
SESSION_DIR = "session"
INTENT_DIR = "intent"
EVENTS_FILE = "events.jsonl"
LOCK_FILE = "lock"
# The config snapshot a run starts with, and the state file name when the
# snapshot does not say. ``LoopConfig.state_file`` defaults to the same name.
SNAPSHOT_FILE = "config.json"
DEFAULT_STATE_FILE = "state.json"
ACTIVE = ("running", "awaiting_human")
INTENT_TTL_S = 60
EVENTS_MAX_BYTES = 1024 * 1024
STATE_MAX_BYTES = 16 << 20
SNAPSHOT_MAX_BYTES = 1 << 20
_MAX_ENTRY_BYTES = 8192
# A hook gives up on a held lock quickly; a person's command waits longer.
HOOK_LOCK_S = 0.15
PERSON_LOCK_S = 10.0
_LOCK_WAIT_S = 0.05

# The CLI the plugin puts on the agent's PATH, and the subcommands only a
# person may run: they clear or end a pause.
CLI_NAME = "delivery-loop"
HUMAN_ONLY = ("resume", "abort")
README_URL = "https://github.com/crblabs/delivery-loop#"
_SEPARATORS = (";", "&&", "||", "|", "&", "\n")
# The CLI named with resume or abort anywhere in one simple command, including
# inside ``bash -c "..."``, ``$(...)`` or after options such as ``--session``.
# The name counts as the CLI when a space follows it, or as the plugin's own
# slash command (``<name>:pipeline``), so a path or a commit message that only
# mentions the project does not match.
# The CLI's options that take a value, so the subcommand is found past them.
_OPTIONS_WITH_VALUE = ("--session", "--config")
# How deep nested shells are followed.
_NEST_MAX = 3
# The plugin's slash command, as a nested session would be given it.
_SLASH = "/%s:pipeline" % CLI_NAME
_WRAPPERS = ("command", "exec", "env", "nohup", "time")
_KEY_LEN = 12


class LockTimeout(RuntimeError):
    """The run directory stayed locked through every retry."""


def old_python() -> bool:
    return sys.version_info[:2] < FLOOR


def old_python_message() -> str:
    floor = "%d.%d" % FLOOR
    found = "%d.%d" % sys.version_info[:2]
    return (
        "delivery-loop needs python3 >= %s, found %s. Put %s or later first on PATH, "
        "or end the run: `delivery-loop abort` in a terminal. See %sprerequisites"
        % (floor, found, floor, README_URL)
    )


def in_terminal() -> bool:
    """Whether a person is at this command: both ends of it are a terminal."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def state_home() -> str:
    return os.path.expanduser(os.environ.get(HOME_ENV) or DEFAULT_HOME)


def worktree_root(start: str) -> str | None:
    """The real path of the first directory at or above ``start`` holding a ``.git``.

    A file ``.git`` counts as well as a directory, so a linked worktree, a
    submodule and a nested repository are each their own root.
    """
    path = os.path.realpath(start)
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    while True:
        if os.path.lexists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def worktree_key(root: str) -> str:
    return hashlib.sha256(os.path.realpath(root).encode("utf-8")).hexdigest()[:_KEY_LEN]


def _safe_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 128
        and all(c.isalnum() or c in "-_." for c in value)
        and value not in (".", "..")
    )


def index_path(key: str) -> str:
    return os.path.join(state_home(), INDEX_DIR, key)


def session_path(session_id: str) -> str:
    return os.path.join(state_home(), INDEX_DIR, SESSION_DIR, session_id)


def intent_path(key: str) -> str:
    return os.path.join(state_home(), INTENT_DIR, key)


def read_json_file(path: str, limit: int = _MAX_ENTRY_BYTES) -> object:
    """A small regular file's JSON, never following a link; ``None`` when absent or bad."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        data = os.read(fd, limit + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def write_json_atomic(path: str, data: object) -> None:
    """Replace ``path`` with ``data`` in one step, so a reader never sees half a file."""
    import tempfile

    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            # On disk before the rename, so a crash leaves the old file or the
            # new one, never an empty one the hooks would read as corrupt.
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _remove(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def read_entry(path: str) -> dict | None:
    entry = read_json_file(path)
    if not isinstance(entry, dict) or not isinstance(entry.get("run_dir"), str):
        return None
    if not os.path.isabs(entry["run_dir"]):
        return None
    return entry


def write_entry(key: str, run_dir: str, worktree: str, session_id: str | None) -> None:
    entry = {"run_dir": run_dir, "worktree": worktree, "session_id": session_id}
    write_json_atomic(index_path(key), entry)
    if session_id is not None and _safe_id(session_id):
        write_json_atomic(session_path(session_id), entry)


def remove_entry(key: str, session_id: object = None) -> None:
    _remove(index_path(key))
    remove_session(session_id)


def remove_session(session_id: object) -> None:
    if _safe_id(session_id):
        _remove(session_path(str(session_id)))


def remove_entries_for(run_dir: str) -> int:
    """Delete every index entry, worktree or session, that names ``run_dir``."""
    removed = 0
    base = os.path.join(state_home(), INDEX_DIR)
    for directory in (base, os.path.join(base, SESSION_DIR)):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            path = os.path.join(directory, name)
            entry = read_entry(path) if os.path.isfile(path) else None
            if entry is not None and entry["run_dir"] == run_dir:
                _remove(path)
                removed += 1
    return removed


def find_entry(
    cwd: object, target: object = None, session_id: object = None
) -> tuple[str, dict] | None:
    """The first run a hook call belongs to (see ``find_entries``), or ``None``."""
    found = find_entries(cwd, target, session_id)
    return found[0] if found else None


def find_entries(cwd: object, target: object = None, session_id: object = None) -> list:
    """Every run a hook call touches, as (worktree key, index entry), without repeats.

    The session's own run, the run of the edited path's worktree and the run of
    ``cwd``'s worktree can all differ: a session bound to one run may edit
    another run's worktree, and each run's rules apply to that edit.
    """
    found = []
    seen = set()
    if _safe_id(session_id):
        entry = read_entry(session_path(str(session_id)))
        if entry is not None and isinstance(entry.get("worktree"), str):
            key = worktree_key(entry["worktree"])
            found.append((key, entry))
            seen.add(key)
    for start in (target, cwd):
        if not isinstance(start, str) or not start:
            continue
        if not os.path.isabs(start) and isinstance(cwd, str):
            start = os.path.join(cwd, start)
        root = worktree_root(start)
        if root is None:
            continue
        key = worktree_key(root)
        if key in seen:
            continue
        entry = read_entry(index_path(key))
        if entry is not None:
            found.append((key, entry))
            seen.add(key)
    return found


def write_intent(key: str, session_id: str) -> None:
    if _safe_id(session_id):
        write_json_atomic(intent_path(key), {"session_id": session_id, "at": time.time()})


def take_intent(key: str, now: float | None = None) -> str | None:
    """The session that is starting a run in this worktree, used once, if still fresh."""
    path = intent_path(key)
    intent = read_json_file(path)
    _remove(path)
    if not isinstance(intent, dict) or not _safe_id(intent.get("session_id")):
        return None
    at = intent.get("at")
    current = time.time() if now is None else now
    if not isinstance(at, (int, float)) or not 0 <= current - at <= INTENT_TTL_S:
        return None
    return intent["session_id"]


# --- Commands in a shell line -------------------------------------------------


def _segments(command: str) -> list[list[str]]:
    """Each simple command in a shell line, as its words. Best effort, never raises."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        words = list(lexer)
    except ValueError:
        words = command.split()
    out: list[list[str]] = [[]]
    for word in words:
        if word in _SEPARATORS or (word and set(word) <= set(";&|")):
            out.append([])
        else:
            out[-1].append(word)
    return [s for s in out if s]


def _strip_prefix(words: list[str]) -> list[str]:
    """Drop variable assignments and wrappers such as ``env`` or ``command``."""
    i = 0
    while i < len(words):
        word = words[i]
        assignment = "=" in word and word.split("=", 1)[0].isidentifier()
        if assignment or word in _WRAPPERS:
            i += 1
        else:
            break
    rest = words[i:]
    if rest and os.path.basename(rest[0]).startswith("python") and len(rest) > 1:
        rest = rest[1:]
    return rest


_PROGRAM_WORDS = {}


def _program_word(program: str) -> "re.Pattern":
    """The program's name as a word on its own: not part of a longer name or a
    directory in a path, which names it without running it."""
    if program not in _PROGRAM_WORDS:
        _PROGRAM_WORDS[program] = re.compile(r"(?<![\w.-])%s(?![\w./-])" % re.escape(program))
    return _PROGRAM_WORDS[program]


def _calls(command: str, program: str) -> list[list[str]]:
    """Every invocation of ``program`` in a shell line, as its arguments.

    The line is only lexed when it names the program: every shell call in every
    session passes through here, and a long heredoc is slow to lex.
    """
    if program not in command or not _program_word(program).search(command):
        return []
    calls = []
    for segment in _segments(command):
        words = _strip_prefix(segment)
        if words and os.path.basename(words[0]) == program:
            calls.append(words[1:])
    return calls


def cli_calls(command: str) -> list[list[str]]:
    """Every invocation of the plugin CLI in a shell line, as its arguments."""
    return _calls(command, CLI_NAME)


def gh_calls(command: str) -> list[list[str]]:
    """Every GitHub CLI invocation in a shell line, as its arguments after ``gh``."""
    return _calls(command, "gh")


def git_calls(command: str) -> list[list[str]]:
    """Every git invocation in a shell line, as its arguments after ``git``."""
    return _calls(command, "git")


def subcommand(args: list[str]) -> str | None:
    """The CLI's subcommand among its arguments, past its global options."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _OPTIONS_WITH_VALUE else 1
    return args[i] if i < len(args) else None


def _human_only_in(words: list[str], module: str | None) -> str | None:
    """resume or abort when one simple command runs the CLI with it.

    The CLI may sit at any word, behind a wrapper the guard does not know
    (``sudo``, ``xargs``, ``timeout 5``, ``python3 -u``, a subshell's ``(``), and
    may be the module that implements it (``-m`` or its file). A quoted word is
    data, not a command, unless it is the plugin's slash command, which a nested
    agent session given it as a prompt runs.
    """
    for i, word in enumerate(words):
        rest = None
        if os.path.basename(word.lstrip("({")) == CLI_NAME or word.lstrip("({") == _SLASH:
            rest = words[i + 1 :]
        elif module and word == "-m" and words[i + 1 : i + 2] == [module]:
            rest = words[i + 2 :]
        elif module and word.replace("\\", "/").endswith(module.replace(".", "/") + ".py"):
            rest = words[i + 1 :]
        elif _SLASH in word and " " in word.strip():
            tokens = word.split()
            for j, token in enumerate(tokens):
                if token.lstrip("/") == _SLASH.lstrip("/"):
                    sub = subcommand(tokens[j + 1 :])
                    if sub in HUMAN_ONLY:
                        return sub
        if rest is not None:
            sub = subcommand(rest)
            if sub is not None and sub.rstrip(")};") in HUMAN_ONLY:
                return sub.rstrip(")};")
    return None


def _nested(command: str) -> list[str]:
    """The shell text a line runs in a nested shell: the script after a ``-c``
    option (``bash -c``, ``script -qc``), and each ``$(...)`` or backtick body."""
    found = []
    for words in _segments(command):
        for i, word in enumerate(words[:-1]):
            if word.startswith("-") and not word.startswith("--") and "c" in word[1:]:
                found.append(words[i + 1])
    at = command.find("$(")
    while at != -1:
        close = command.find(")", at + 2)
        found.append(command[at + 2 : close if close != -1 else len(command)])
        at = command.find("$(", at + 2)
    found += command.split("`")[1::2]
    return found


def human_only_command(command: object, module: str | None = None, depth: int = 0) -> str | None:
    """The subcommand when a shell line runs the CLI with resume or abort, else ``None``.

    Read from the parsed words, so a commit message or an ``echo`` that only
    mentions the CLI does not match, and from every nested shell (``bash -c``,
    ``script -qc``, ``$(...)``), so wrapping the call does not hide it. This is
    the early, readable refusal; the CLI itself also refuses both outside a
    terminal.
    """
    if not isinstance(command, str) or depth > _NEST_MAX:
        return None
    named = _program_word(CLI_NAME).search(command) or _SLASH in command
    if not named and not (module and module.rsplit(".", 1)[-1] in command):
        return None
    for words in _segments(command):
        sub = _human_only_in(words, module)
        if sub is not None:
            return sub
    for inner in _nested(command):
        sub = human_only_command(inner, module, depth + 1)
        if sub is not None:
            return sub
    return None


def starts_run(command: object) -> bool:
    """Whether a shell line may start a run: ``start`` past the global options,
    or a call that reads its subcommand from stdin."""
    if not isinstance(command, str):
        return False
    for args in cli_calls(command):
        sub = subcommand(args)
        if sub == "start" or (sub is None and "--args-stdin" in args):
            return True
    return False


# --- Run directory: lock, events, abort ---------------------------------------


@contextlib.contextmanager
def locked(run_dir: str, timeout: float = HOOK_LOCK_S):
    """Hold the run directory's lock, retrying up to ``timeout`` seconds, else
    raise ``LockTimeout``."""
    import fcntl

    os.makedirs(run_dir, exist_ok=True)
    fd = os.open(os.path.join(run_dir, LOCK_FILE), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        "the run directory %s stayed locked; another hook or command is "
                        "writing it. Retry in a moment." % run_dir
                    ) from exc
                time.sleep(_LOCK_WAIT_S)
        yield
    finally:
        os.close(fd)


def now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def append_event(run_dir: str, record: dict) -> None:
    """One line in the run's event log, rotated once it passes ``EVENTS_MAX_BYTES``."""
    path = os.path.join(run_dir, EVENTS_FILE)
    try:
        if os.path.getsize(path) > EVENTS_MAX_BYTES:
            os.replace(path, path + ".1")
    except OSError:
        pass
    line = json.dumps(dict({"at": now_iso()}, **record), sort_keys=True)
    with contextlib.suppress(OSError), open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def read_events(run_dir: str, count: int) -> list[dict]:
    try:
        with open(os.path.join(run_dir, EVENTS_FILE), encoding="utf-8") as handle:
            lines = handle.readlines()[-count:]
    except OSError:
        return []
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


def state_file(run_dir: str) -> str:
    """The run's state file, named in the config snapshot ``start`` wrote."""
    snapshot = read_json_file(os.path.join(run_dir, SNAPSHOT_FILE), SNAPSHOT_MAX_BYTES)
    name = snapshot.get("state_file") if isinstance(snapshot, dict) else None
    if not isinstance(name, str) or "/" in name or name in ("", ".", ".."):
        name = DEFAULT_STATE_FILE
    return os.path.join(run_dir, name)


def abort_run(key: str, entry: dict, by: str, timeout: float = PERSON_LOCK_S) -> str:
    """End the run as ``failed`` with ``ended_reason: aborted`` and drop its index.

    The one abort, for every interpreter: the CLI on a supported Python and the
    old-Python path both call it, under the same lock as every other writer.
    """
    run_dir = entry["run_dir"]
    path = state_file(run_dir)
    with locked(run_dir, timeout):
        state = read_json_file(path, STATE_MAX_BYTES)
        if isinstance(state, dict) and state.get("status") in ACTIVE:
            state["status"] = "failed"
            state["ended_reason"] = "aborted"
            revision = state.get("revision")
            state["revision"] = revision + 1 if isinstance(revision, int) else 1
            state["updated_at"] = now_iso()
            history = state.get("history")
            if isinstance(history, list):
                history.append({"at": state["updated_at"], "event": "aborted", "by": by})
            write_json_atomic(path, state)
        remove_entry(key, entry.get("session_id"))
        append_event(run_dir, {"hook": "cli", "decision": "abort", "by": by})
    return "Aborted the run in %s. Its state is kept in %s." % (entry.get("worktree"), run_dir)
