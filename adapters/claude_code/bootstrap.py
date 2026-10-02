"""What Claude Code's hooks and the CLI do before anything needs the 3.11 floor.

The plugin's hook shims and ``bin/delivery-loop`` import this module first. It
parses on Python 3.8 and imports only the standard library and
``core.run_index``, so it works on whatever ``python3`` a machine ships. It holds
the Claude Code knowledge that path needs: the tool names, the payload fields,
the output shapes, the plugin's slash command and the session variable.

  * ``payload_of`` and the ``*_json`` builders: the one parser of a hook payload
    and the one writer of each output shape, for the shims and the adapter.
  * ``needs_adapter``: whether a PreToolUse call can be answered without loading
    the adapter. Most calls on a machine belong to no run, so they return here.
  * ``human_only_call``: a Bash or Skill call that would resume or abort a run.
    The CLI refuses both outside a terminal anyway; this is the early, readable
    refusal.
  * ``prompt_command``: a typed ``/delivery-loop:pipeline resume|abort``. Only
    a person types a prompt, so the prompt hook is where a resume or an abort
    from inside Claude Code is applied.
  * ``old_python_hook`` and ``old_python_cli``: what the hooks and the CLI do on
    an interpreter older than the floor. With no run, nothing. With a run, the
    guard denies edits, the Stop hook blocks once per turn, and a person can
    still abort it, from the prompt or a terminal.
"""

from __future__ import annotations

import json
import os
import re
import sys

from core import run_index as ri

COMMAND_NAME = "delivery-loop:pipeline"
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SESSION_ENV = "CLAUDE_SESSION_ID"
# A code host's MCP tool that writes to the remote repository. Its name is split
# into words (snake, kebab or camel case); a tool that starts or ends with a read
# verb is a read, and any other one that names a repository object is judged by
# the publish policy, not as a local file. A local git server's tools are not.
_MCP_HOST_RE = re.compile(r"^mcp__.*(github|gitlab|bitbucket|gitea).*__", re.I)
_WORD_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
_READ_VERBS = ("get", "list", "search", "read", "view", "diff", "compare", "download", "fetch")
_REPO_OBJECTS = (
    "file",
    "files",
    "branch",
    "branches",
    "pull",
    "merge",
    "commit",
    "commits",
    "release",
    "releases",
    "repository",
    "repo",
    "tag",
    "tags",
    "ref",
    "refs",
    "fork",
    "push",
    "content",
    "contents",
    "tree",
)
# A Linear MCP tool that writes to the tracker: an issue, a comment, a project,
# a label, a document. Its reads (get, list, search) pass. During a run every
# write is denied: loop-issue is the one write path, and only outside a run.
_MCP_TRACKER_WRITE_RE = re.compile(
    r"^mcp__.*linear.*__(save|create|delete|update|share|unshare|merge|retire|restore|submit"
    r"|resolve|mark|prepare|archive|unarchive|add|remove|set|move|assign|link|unlink)_",
    re.I,
)
# The input fields that name the branch a code host's write goes to.
_BRANCH_FIELDS = ("branch", "head")
# An MCP tool whose name says it writes, and the input fields that name a path.
_MCP_WRITE_RE = re.compile(r"^mcp__.*(write|edit|create|move|rename|delete|patch|replace)", re.I)
_PATH_FIELDS = (
    "file_path",
    "notebook_path",
    "path",
    "source",
    "destination",
    "target",
    "old_path",
    "new_path",
)
# The CLI module, which can also be run directly, as ``python3 -m`` or by its file.
_CLI_MODULE = "adapters.claude_code.cli"


def payload_of(raw: str) -> dict | None:
    try:
        payload = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return payload if isinstance(payload, dict) else None


def deny_json(reason: str) -> str:
    out = {"hookEventName": "PreToolUse", "permissionDecision": "deny"}
    out["permissionDecisionReason"] = reason
    return json.dumps({"hookSpecificOutput": out})


def block_json(reason: str) -> str:
    return json.dumps({"decision": "block", "reason": reason})


def system_message_json(text: str) -> str:
    """A note the harness shows the person, without blocking anything."""
    return json.dumps({"systemMessage": text})


def context_json(text: str) -> str:
    out = {"hookEventName": "UserPromptSubmit", "additionalContext": text}
    return json.dumps({"hookSpecificOutput": out})


def kind_of(tool_name: object) -> str:
    """``edit`` for a tool that writes a file, ``shell`` for Bash, ``tracker`` for
    an issue tracker's write, ``publish`` for a code host's write, else ``other``."""
    if tool_name in EDIT_TOOLS:
        return "edit"
    if tool_name == "Bash":
        return "shell"
    if not isinstance(tool_name, str):
        return "other"
    if _MCP_TRACKER_WRITE_RE.match(tool_name):
        return "tracker"
    if _MCP_HOST_RE.match(tool_name):
        words = [w.lower() for w in _WORD_RE.findall(tool_name.rsplit("__", 1)[-1])]
        read = bool(words) and (words[0] in _READ_VERBS or words[-1] in _READ_VERBS)
        if not read and any(w in _REPO_OBJECTS for w in words):
            return "publish"
    if _MCP_WRITE_RE.match(tool_name):
        return "edit"
    return "other"


def targets_of(payload: dict) -> list:
    """Every path an edit tool names: a move's source as well as its destination."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or kind_of(payload.get("tool_name")) != "edit":
        return []
    values = [tool_input.get(field) for field in _PATH_FIELDS]
    return list(dict.fromkeys(v for v in values if isinstance(v, str) and v))


def target_of(payload: dict) -> object:
    targets = targets_of(payload)
    return targets[0] if targets else None


def branch_of(payload: dict) -> object:
    """The branch a code host's write names, for the publish policy."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or kind_of(payload.get("tool_name")) != "publish":
        return None
    for field in _BRANCH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def human_only_call(tool_name: object, tool_input: object) -> str | None:
    """The subcommand when a tool call would resume or abort a run, else ``None``."""
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "Bash":
        return ri.human_only_command(tool_input.get("command"), _CLI_MODULE)
    if tool_name == "Skill" and isinstance(tool_input.get("skill"), str):
        skill = tool_input["skill"].lstrip("/")
        words = str(tool_input.get("args") or "").split()
        if skill == COMMAND_NAME:
            for word in words:
                if word in ri.HUMAN_ONLY:
                    return word
    return None


def human_only_message(sub: str) -> str:
    return (
        "Denied: `%s` clears or ends a pause, so only a person may run it. Cause: a run "
        "pauses for a human decision. Fix: stop and ask the person to type "
        "`/%s %s`. See %swhat-the-guard-covers" % (sub, COMMAND_NAME, sub, ri.README_URL)
    )


# Only the namespaced command: a short ``/pipeline`` may be another plugin's or a
# leftover in-repo command, and must not resume or end this plugin's run.
_PROMPT_NAMES = (COMMAND_NAME,)


def prompt_command(prompt: object) -> str | None:
    """The subcommand when a person typed ``/delivery-loop:pipeline resume|abort``."""
    if not isinstance(prompt, str):
        return None
    words = prompt.strip().split()
    if len(words) < 2 or not words[0].startswith("/"):
        return None
    if words[0][1:].casefold() not in _PROMPT_NAMES:
        return None
    sub = words[1].casefold()
    return sub if sub in ri.HUMAN_ONLY else None


def needs_adapter(payload: dict) -> bool:
    """Whether a PreToolUse call needs the adapter: a run, a resume, or a start."""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if human_only_call(payload.get("tool_name"), tool_input) is not None:
        return True
    if payload.get("tool_name") == "Bash" and ri.starts_run(tool_input.get("command")):
        return True
    return any(
        ri.find_entry(payload.get("cwd"), target, payload.get("session_id")) is not None
        for target in targets_of(payload) or [None]
    )


def run_found(payload: dict) -> bool:
    return ri.find_entry(payload.get("cwd"), None, payload.get("session_id")) is not None


def old_python_hook(kind: str, payload: dict) -> dict | None:
    """What a hook does when ``python3`` is older than the floor.

    No run: nothing, the call passes. A run: the guard denies edit tools and the
    Stop hook blocks once per turn for the bound session, both naming the fix. A
    typed abort still ends the run; a typed resume says what to fix first.
    """
    if kind == "guard":
        sub = human_only_call(payload.get("tool_name"), payload.get("tool_input"))
        if sub is not None:
            return {"deny": human_only_message(sub)}
    if kind == "prompt":
        sub = prompt_command(payload.get("prompt"))
        if sub is None:
            return None
        found = ri.find_entry(payload.get("cwd"), None, payload.get("session_id"))
        if found is None:
            return {"context": "No active delivery-loop run in this worktree."}
        if sub == "abort":
            try:
                return {"context": ri.abort_run(found[0], found[1], "person")}
            except (ri.LockTimeout, OSError) as exc:
                return {"context": "delivery-loop could not abort the run: %s" % exc}
        return {"context": ri.old_python_message()}
    found = ri.find_entry(payload.get("cwd"), target_of(payload), payload.get("session_id"))
    if found is None:
        return None
    entry = found[1]
    if kind == "guard":
        # Without the full guard, every call that writes or publishes waits for
        # a supported python3.
        tool = payload.get("tool_name")
        tool_input = payload.get("tool_input")
        command = tool_input.get("command") if isinstance(tool_input, dict) else None
        publishes = isinstance(command, str) and (
            any(a[:1] == ["push"] for a in ri.git_calls(command))
            or any(a[:2] == ["pr", "merge"] or a[:1] == ["api"] for a in ri.gh_calls(command))
        )
        if kind_of(tool) in ("edit", "publish", "tracker") or publishes:
            return {"deny": ri.old_python_message()}
        return None
    if payload.get("stop_hook_active") is True:
        return None
    state = ri.read_json_file(ri.state_file(entry["run_dir"]), ri.STATE_MAX_BYTES)
    if isinstance(state, dict) and state.get("status") != "running":
        # A paused run waits for a person; blocking would only re-prompt the agent.
        return None
    bound = entry.get("session_id")
    if bound is not None and bound != payload.get("session_id"):
        return None
    return {"block": ri.old_python_message()}


def old_python_cli(argv: list) -> int:
    """``delivery-loop`` on an interpreter older than the floor: abort and status only."""
    import argparse

    parser = argparse.ArgumentParser(prog=ri.CLI_NAME, add_help=False, allow_abbrev=False)
    parser.add_argument("--session")
    parser.add_argument("--args-stdin", action="store_true")
    options, rest = parser.parse_known_args(argv)
    if options.args_stdin:
        # The slash command passes what a person typed on stdin.
        rest = sys.stdin.read().split() + rest
    sub = rest[0] if rest else ""
    if sub not in ("abort", "status"):
        sys.stderr.write(ri.old_python_message() + "\n")
        return 2
    session = options.session or os.environ.get(SESSION_ENV)
    found = ri.find_entry(os.getcwd(), None, session)
    if found is None:
        print("No active delivery-loop run in this worktree.")
        return 0
    key, entry = found
    if sub == "abort":
        if not ri.in_terminal():
            sys.stderr.write(
                "abort is for a person: type /%s abort in Claude Code, or run it in a "
                "terminal.\n" % COMMAND_NAME
            )
            return 2
        try:
            print(ri.abort_run(key, entry, "terminal"))
        except (ri.LockTimeout, OSError) as exc:
            sys.stderr.write("delivery-loop could not abort the run: %s\n" % exc)
            return 2
        return 0
    state = ri.read_json_file(ri.state_file(entry["run_dir"]), ri.STATE_MAX_BYTES)
    if not isinstance(state, dict):
        print("Run in %s: its state file cannot be read." % entry.get("worktree"))
        return 0
    print(
        "Task %s in %s\nStatus: %s, stage %s\nRun directory: %s"
        % (
            state.get("task"),
            entry.get("worktree"),
            state.get("status"),
            state.get("current_stage"),
            entry["run_dir"],
        )
    )
    sys.stderr.write(ri.old_python_message() + "\n")
    return 0
