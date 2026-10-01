"""What the loop allows before a tool runs: capability 2 of ``docs/adapter-contract.md``.

``handle_pre_tool`` decides one tool call of a session in an active run's
worktree. It holds three rules:

  * An edit may not write where the run's own rules live: the plugin, the
    harness's user directory (``config.HARNESS_HOME``, which holds the user
    settings and the installed plugins) and the state home. Other paths outside
    the worktree are allowed, because a stage's command may keep its own files
    there (a plan under ``~/.gstack``, for one). Inside the worktree, a carve-out
    is always denied, and any other loop file (under ``loop_prefixes`` or in
    ``loop_exact``) only when the approved plan declared it. Every other file is
    ordinary source and is allowed. The path is checked as written and as it
    resolves, so a symlink cannot lead an edit to a guarded file.
  * ``delivery-loop resume`` and ``abort`` clear or end a pause, so only a
    person may run them. The adapter checks every route to them before any run
    is found, with ``core.run_index.human_only_command`` for a shell line.
  * ``git push`` is allowed in the stage that publishes (the one whose ``emits``
    names a ``PR``), for the run's own branch, to ``origin``. A force push, a
    push of another branch, ``--all``, ``--mirror``, ``--delete`` or a ``+``
    refspec is denied. This policy reads the command text, so it is advisory: a
    determined shell line can hide a push. The README says so.

Nothing else in a shell command is checked here; the turn-end guard map catches
a shell edit of a guarded file after the fact.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from core import run_index as ri
from core import run_state as rs
from core.config import (
    HARNESS_HOME,
    HARNESS_HOME_ENV,
    SETTINGS_FILES,
    user_config_dir,
    user_git_configs,
)
from core.pipeline_loop_paths import classify_loop_path, is_carveout

_GUARD_DOC = f"{ri.README_URL}what-the-guard-covers"
_FORBIDDEN_PUSH_FLAGS = (
    "--force",
    "--force-with-lease",
    "--force-if-includes",
    "--all",
    "--branches",
    "--mirror",
    "--delete",
    "--prune",
    "--repo",
    "--receive-pack",
    "--exec",
    "--tags",
    "--follow-tags",
)
# git accepts any unique prefix of a long option, so --del means --delete; from
# two letters after the dashes a prefix is taken as the option it could be.
_MIN_PREFIX = 2
# Single-letter push flags that force or delete; a cluster such as -uf holds one.
_FORBIDDEN_SHORT = set("fd")
# Push options that take the next word as their value.
_PUSH_VALUE_OPTIONS = ("-o", "--push-option", "--receive-pack", "--exec", "--repo")
# git options that take a value as the next word, before the subcommand.
_GIT_VALUE_OPTIONS = ("-c", "-C", "--git-dir", "--work-tree", "--namespace", "--config-env")
# git options before the subcommand that point a push at another repository.
_GIT_ELSEWHERE = ("-C", "--git-dir", "--work-tree")
# Environment variables that set git config or point git at another repository.
_GIT_ENV_RE = re.compile(r"(?<![\w])GIT_(?:CONFIG\w*|DIR|WORK_TREE|SSH\w*|EXEC_PATH)=")
# A ``-c`` setting that changes where a push goes or what it runs.
_GIT_PUSH_SETTINGS = ("remote.", "url.", "push.", "core.sshcommand", "credential.")
# Where a project's settings file sits, whatever the directory above it.
_SETTINGS_TAILS = tuple("/" + rel.casefold() for rel in SETTINGS_FILES)


@dataclass(frozen=True)
class PreToolCall:
    """One tool call, in the loop's terms. ``kind`` is ``edit`` for a tool that
    writes ``path``, ``shell`` for one that runs ``command``, ``publish`` for one
    that writes to the remote repository itself, ``other`` otherwise;
    ``tool_name`` is the harness's own name, for messages only. ``branch`` is the
    branch a ``publish`` call names, if it names one."""

    session_id: str | None
    kind: str
    tool_name: str
    cwd: str | None
    command: str | None
    path: str | None
    branch: str | None = None


@dataclass(frozen=True)
class PreToolVerdict:
    allow: bool
    reason: str | None = None


ALLOW = PreToolVerdict(allow=True)


def _deny(problem: str, cause: str, fix: str) -> PreToolVerdict:
    return PreToolVerdict(
        allow=False, reason=f"Denied: {problem}. Cause: {cause}. Fix: {fix}. See {_GUARD_DOC}"
    )


def protected_roots(run: rs.Run) -> list[Path]:
    """Directories outside any worktree that an edit tool may never write."""
    roots = [rs.PLUGIN_ROOT, Path(ri.state_home()), Path(HARNESS_HOME).expanduser()]
    roots.append(Path(run.config.state_root).expanduser())
    # The operator's loop config for every run of a repository, and the git
    # config files a push reads before the repository's own.
    roots.append(user_config_dir())
    roots += user_git_configs()
    if os.environ.get(HARNESS_HOME_ENV):
        roots.append(Path(os.environ[HARNESS_HOME_ENV]).expanduser())
    return [Path(os.path.realpath(r)) for r in roots]


def _under(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or inside it, ignoring letter case.

    A case-insensitive file system, the default on macOS, names one file with
    any spelling, so a guard that compares case-sensitively can be stepped past.
    """
    folded, base = str(path).casefold(), str(root).casefold().rstrip("/")
    return folded == base or folded.startswith(base + "/")


def _is_settings_file(path: Path) -> bool:
    """A project settings file the harness reads, in any directory: launched from
    a parent directory, the harness reads the one there, outside the worktree."""
    return str(path).casefold().endswith(_SETTINGS_TAILS)


def _absolute(path: str, cwd: str | None, worktree: Path) -> tuple[Path, Path]:
    raw = Path(path).expanduser()
    raw = raw if raw.is_absolute() else Path(cwd or worktree) / raw
    return Path(os.path.normpath(raw)), Path(os.path.realpath(raw))


def _relative(lexical: Path, resolved: Path, worktree: Path) -> tuple[str | None, str | None]:
    """The path relative to the worktree, as written and as resolved; ``None`` outside.

    Compared ignoring letter case, as ``_under`` is: on a case-insensitive file
    system ``/Users/x/REPO/loop.toml`` is the worktree's own ``loop.toml``.
    """
    roots = [Path(os.path.realpath(worktree)), Path(worktree)]
    out = []
    for candidate in (lexical, resolved):
        rel = None
        for root in roots:
            if _under(candidate, root):
                text, base = str(candidate), str(root).rstrip("/")
                rel = text[len(base) + 1 :] if len(text) > len(base) else ""
                break
        out.append(rel)
    return out[0], out[1]


def _in_loop_region(rel: str, run: rs.Run) -> bool:
    config = run.config
    folded = rel.casefold()
    return (
        is_carveout(rel, config)
        or folded in {e.casefold() for e in config.loop_exact}
        or folded.startswith(tuple(p.casefold() for p in config.loop_prefixes))
    )


def _check_edit(call: PreToolCall, run: rs.Run, state: dict) -> PreToolVerdict:
    if not call.path:
        return ALLOW
    absolute = _absolute(call.path, call.cwd, run.worktree)
    worktree = Path(os.path.realpath(run.worktree))
    for path in absolute:
        inside = _under(path, worktree)
        for root in protected_roots(run):
            # A protected directory that holds the whole worktree, such as a
            # repository kept under the harness home, does not lock the
            # worktree's own files: the carve-outs below judge those. A root
            # equal to the worktree in any spelling (the plugin's own checkout)
            # stays protected.
            if inside and _under(worktree, root) and not _under(root, worktree):
                continue
            if _under(path, root):
                return _deny(
                    f"{call.tool_name} of {call.path}",
                    f"it is under {root}, which holds the loop's own rules or state",
                    "leave it unchanged; a person changes it outside the run",
                )
        if _is_settings_file(path):
            return _deny(
                f"{call.tool_name} of {call.path}",
                "a project settings file can switch the loop's hooks off",
                f"leave it unchanged; a person edits it outside the run, or types "
                f"{run.config.abort_command}",
            )
    lexical, resolved = _relative(*absolute, run.worktree)
    declared = state.get("declared_loop_edits") or []
    for rel in dict.fromkeys((lexical, resolved)):
        if rel is None or rel in ("", "."):
            continue
        if not _in_loop_region(rel, run):
            continue
        verdict = classify_loop_path(rel, declared, run.config)
        if verdict == "denied-carveout":
            return _deny(
                f"{call.tool_name} of {rel}",
                "it is an enforcement file no plan can authorize (a carve-out)",
                f"leave it unchanged; a person edits it outside the run, or runs "
                f"{run.config.abort_command}",
            )
        if verdict == "not-declared":
            return _deny(
                f"{call.tool_name} of {rel}",
                "it is a loop file the approved plan did not declare",
                "add it to the plan's ```loop-edits``` block and have the plan approved again",
            )
    return ALLOW


def _push_target(args: list[str]) -> tuple[list[str], list[str], list[str]] | None:
    """(git ``-c`` settings, push flags, positional words) of a ``git push``, or
    ``None`` when the git call is not a push."""
    settings, i = [], 0
    while i < len(args) and args[i].startswith("-"):
        name, _, inline = args[i].partition("=")
        value = inline or (args[i + 1] if i + 1 < len(args) else "")
        if name in ("-c", "--config-env"):
            settings.append(value)
        elif name in _GIT_ELSEWHERE:
            settings.append(f"{name}={value}")
        i += 2 if args[i] in _GIT_VALUE_OPTIONS else 1
    if i >= len(args) or args[i] != "push":
        return None
    flags, words, rest = [], [], args[i + 1 :]
    j = 0
    while j < len(rest):
        word = rest[j]
        if word.startswith("-"):
            flags.append(word)
            if word in _PUSH_VALUE_OPTIONS:
                j += 1
        else:
            words.append(word)
        j += 1
    return settings, flags, words


def _forbidden_flags(flags: list[str]) -> list[str]:
    bad = []
    for flag in flags:
        name = flag.split("=", 1)[0]
        if name.startswith("--"):
            abbreviates = len(name) >= _MIN_PREFIX + 2 and any(
                f.startswith(name) for f in _FORBIDDEN_PUSH_FLAGS
            )
            if name in _FORBIDDEN_PUSH_FLAGS or abbreviates:
                bad.append(flag)
        elif _FORBIDDEN_SHORT & set(name[1:]):
            bad.append(flag)
    return bad


_MERGE_RE = re.compile(r"merge", re.I)
# A code host's write of repository content, which lands on a branch.
_CONTENT_WRITE_RE = re.compile(r"file|commit|push", re.I)
# The REST endpoints that merge: a pull request's /merge, a repository's /merges.
_MERGE_PATH_RE = re.compile(r"/merges?(?:$|[/?])")


def _publishing(run: rs.Run, state: dict) -> bool:
    stage = run.config.stages[state["current"]]
    return stage.emits.split(":", 1)[0].strip().upper() == "PR"


def _check_publish(call: PreToolCall, run: rs.Run, state: dict) -> PreToolVerdict:
    """A tool that writes to the remote directly: only the publishing stage, only
    the run's own branch, and never a merge, which is a person's call."""
    tool = call.tool_name
    branch = state.get("branch")
    if _MERGE_RE.search(tool):
        return _deny(
            tool,
            "a merge lands the work on a shared branch, and that is a person's call",
            "open the pull request and leave the merge to a person",
        )
    if not _publishing(run, state):
        return _deny(
            tool,
            f"stage {state['current_stage']} does not publish; only the stage that opens the "
            "pull request writes to the remote",
            "commit locally and leave publishing to that stage",
        )
    if call.branch is not None and call.branch != branch:
        return _deny(
            f"{tool} on {call.branch}",
            f"it is not the run's branch ({branch})",
            f"name {branch} as the branch",
        )
    if call.branch is None and _CONTENT_WRITE_RE.search(tool):
        return _deny(
            tool,
            "a write that names no branch goes to the default branch",
            f"name {branch} as the branch",
        )
    return ALLOW


def _check_gh(command: str) -> PreToolVerdict | None:
    """``gh pr merge`` or a merge through ``gh api``: never from a run."""
    for args in ri.gh_calls(command):
        merge = args[:2] == ["pr", "merge"] or (
            args[:1] == ["api"] and any(_MERGE_PATH_RE.search(a) for a in args[1:])
        )
        if merge:
            return _deny(
                "gh " + " ".join(args[:2]),
                "a merge lands the work on a shared branch, and that is a person's call",
                "open the pull request and leave the merge to a person",
            )
    return None


def _check_push(command: str, run: rs.Run, state: dict) -> PreToolVerdict:
    branch = state.get("branch")
    for args in ri.git_calls(command):
        target = _push_target(args)
        if target is None:
            continue
        settings, flags, words = target
        if not _publishing(run, state):
            return _deny(
                "git push",
                f"stage {state['current_stage']} does not publish; only the stage that opens "
                "the pull request pushes",
                "commit locally and leave the push to that stage",
            )
        redirect = [s for s in settings if s.startswith(_GIT_ELSEWHERE)]
        redirect += [s for s in settings if s.casefold().startswith(_GIT_PUSH_SETTINGS)]
        if _GIT_ENV_RE.search(command):
            redirect.append("GIT_* environment")
        if redirect:
            return _deny(
                f"git push with {redirect[0]}",
                "a setting, a variable or another directory on the command line can send the "
                "push elsewhere",
                f"push plainly from the worktree: git push -u origin {branch or 'HEAD'}",
            )
        bad = _forbidden_flags(flags)
        if bad:
            return _deny(
                f"git push {' '.join(bad)}",
                "a forced, deleting or all-branch push rewrites what others see",
                f"push the run's branch plainly: git push -u origin {branch or 'HEAD'}",
            )
        remote, refspecs = (words[0], words[1:]) if words else ("origin", [])
        if remote != "origin":
            return _deny(
                f"git push to {remote}",
                "a run publishes to origin only",
                f"git push -u origin {branch or 'HEAD'}",
            )
        if not refspecs and branch != _current_branch(run):
            return _deny(
                "git push of the checked-out branch",
                f"it is not the run's branch ({branch})",
                f"git push -u origin {branch or 'HEAD'}",
            )
        for spec in refspecs:
            source, _, dest = spec.rpartition(":")
            dest = (dest or spec).removeprefix("refs/heads/")
            deleting = ":" in spec and source == ""
            if dest == "HEAD" and branch != _current_branch(run):
                dest = "HEAD (not the run's branch)"
            if spec.startswith("+") or deleting or dest not in ("HEAD", branch):
                return _deny(
                    f"git push of {spec}",
                    f"a run pushes its own branch ({branch}) and nothing else",
                    f"git push -u origin {branch or 'HEAD'}",
                )
    return ALLOW


def _current_branch(run: rs.Run) -> str | None:
    return rs.detect_branch(run.worktree)


def handle_pre_tool(call: PreToolCall, run: rs.Run, state: dict | None = None) -> PreToolVerdict:
    """Allow or deny one tool call in an active run's worktree.

    ``state`` is the run's state when the caller has just read it.
    """
    if state is None:
        _, state = rs.read(run)
    if state is None or state.get("status") not in rs.ACTIVE:
        return ALLOW
    if call.kind == "edit":
        return _check_edit(call, run, state)
    if call.kind == "publish":
        return _check_publish(call, run, state)
    if call.kind == "shell" and call.command:
        return _check_gh(call.command) or _check_push(call.command, run, state)
    return ALLOW
