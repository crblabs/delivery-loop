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
# An MCP tool whose name says it writes, and the input fields that name a path.
_MCP_WRITE_RE = re.compile(r"^mcp__.*(write|edit|create|move|rename|delete|patch|replace)", re.I)
_PATH_FIELDS = ("file_path", "notebook_path", "path", "destination", "target", "new_path")
# The CLI module run directly, as ``python3 -m`` or by its file.
_CLI_MODULE_RE = re.compile(
    r"adapters[./]claude_code[./]cli\b[^\n;&|]*?\b(%s)\b" % "|".join(ri.HUMAN_ONLY)
)
_GLOBAL_WITH_VALUE = ("--session",)
_GLOBAL_FLAGS = ("--args-stdin",)


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
    """``edit`` for a tool that writes a file, ``shell`` for Bash, else ``other``."""
    if tool_name in EDIT_TOOLS:
        return "edit"
    if tool_name == "Bash":
        return "shell"
    if isinstance(tool_name, str) and _MCP_WRITE_RE.match(tool_name):
        return "edit"
    return "other"


def target_of(payload: dict) -> object:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or kind_of(payload.get("tool_name")) != "edit":
        return None
    for field in _PATH_FIELDS:
        value = tool_input.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def split_global(argv: list) -> tuple:
    """(global options as a dict, the rest) from the CLI's arguments."""
    options = {}
    rest = list(argv)
    while rest and rest[0].startswith("--"):
        name, _, value = rest[0].partition("=")
        if name in _GLOBAL_WITH_VALUE:
            if value:
                options[name] = value
                rest = rest[1:]
            elif len(rest) > 1:
                options[name] = rest[1]
                rest = rest[2:]
            else:
                rest = rest[1:]
        elif name in _GLOBAL_FLAGS:
            options[name] = True
            rest = rest[1:]
        else:
            break
    return options, rest


def human_only_call(tool_name: object, tool_input: object) -> str | None:
    """The subcommand when a tool call would resume or abort a run, else ``None``."""
    if not isinstance(tool_input, dict):
        return None
    if tool_name == "Bash":
        command = tool_input.get("command")
        sub = ri.human_only_command(command)
        if sub is None and isinstance(command, str):
            match = _CLI_MODULE_RE.search(command)
            sub = match.group(1) if match else None
        return sub
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


# The forms a person types: the namespaced command, or its short name, which
# Claude Code resolves to the plugin's command when no other command has it.
_PROMPT_NAMES = (COMMAND_NAME, COMMAND_NAME.split(":", 1)[1])


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
    found = ri.find_entry(payload.get("cwd"), target_of(payload), payload.get("session_id"))
    return found is not None


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
            return {"context": ri.abort_run(found[0], found[1], "person")}
        return {"context": ri.old_python_message()}
    found = ri.find_entry(payload.get("cwd"), target_of(payload), payload.get("session_id"))
    if found is None:
        return None
    entry = found[1]
    if kind == "guard":
        if kind_of(payload.get("tool_name")) == "edit":
            return {"deny": ri.old_python_message()}
        return None
    if payload.get("stop_hook_active") is True:
        return None
    bound = entry.get("session_id")
    if bound is not None and bound != payload.get("session_id"):
        return None
    return {"block": ri.old_python_message()}


def old_python_cli(argv: list) -> int:
    """``delivery-loop`` on an interpreter older than the floor: abort and status only."""
    options, rest = split_global(argv)
    if options.get("--args-stdin"):
        # The slash command passes what a person typed on stdin.
        rest = sys.stdin.read().split() + rest
    sub = rest[0] if rest else ""
    if sub not in ("abort", "status"):
        sys.stderr.write(ri.old_python_message() + "\n")
        return 2
    session = options.get("--session") or os.environ.get(SESSION_ENV)
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
        print(ri.abort_run(key, entry, "terminal"))
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
