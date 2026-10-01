"""The plugin and its marketplace, as Claude Code reads them from this repository.

The repository root is the plugin root, so every file Claude Code loads from a
plugin root by default would ship to every user. These tests pin the manifests
and keep those files out.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Loaded from a plugin root by default; none of them may sit at this root.
# bin/ is loaded too, on purpose: it holds the delivery-loop CLI.
AUTO_LOADED = (
    ".mcp.json",
    ".lsp.json",
    "settings.json",
    "agents",
    "output-styles",
    "workflows",
    "themes",
    "monitors",
)


def _json(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def test_the_plugin_manifest_names_the_plugin_and_no_version() -> None:
    # Value: protects=every commit installs as a new version, as the issue asks;
    # fails_when=a version is pinned and users stop receiving commits; why_new=plugin; seam=none
    manifest = _json(".claude-plugin/plugin.json")
    assert manifest["name"] == "delivery-loop"
    assert "version" not in manifest
    assert manifest["license"] == "MIT"


def test_the_marketplace_lists_this_repository_as_the_plugin() -> None:
    market = _json(".claude-plugin/marketplace.json")
    assert market["name"] == "crblabs"
    assert market["owner"]["name"]
    assert market["plugins"] == [
        {
            "name": "delivery-loop",
            "source": "./",
            "description": market["plugins"][0]["description"],
        }
    ]


def test_no_file_claude_code_auto_loads_sits_at_the_plugin_root() -> None:
    # Value: protects=users do not receive this repository's developer MCP server or settings;
    # fails_when=a .mcp.json comes back at the root; why_new=the root is the plugin; seam=none
    present = [name for name in AUTO_LOADED if (ROOT / name).exists()]
    assert present == []


def _hook_commands() -> list[str]:
    hooks = _json("hooks/hooks.json")["hooks"]
    return [h["command"] for groups in hooks.values() for g in groups for h in g["hooks"]]


def test_every_hook_command_names_a_file_in_the_plugin() -> None:
    commands = _hook_commands()
    assert len(commands) == 3
    for command in commands:
        assert '"${CLAUDE_PLUGIN_ROOT}/' in command, "the plugin root must be quoted"
        words = shlex.split(command.replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT)))
        assert words[0] == "python3"
        assert Path(words[1]).is_file(), words[1]


def test_the_guard_sees_every_tool_that_can_edit_or_resume() -> None:
    pre = _json("hooks/hooks.json")["hooks"]["PreToolUse"][0]["matcher"]
    assert set(pre.split("|")) == {
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
        "Bash",
        "Skill",
        "mcp__.*",
    }
    assert set(_json("hooks/hooks.json")["hooks"]) == {"Stop", "PreToolUse", "UserPromptSubmit"}


def test_the_cli_and_the_hooks_are_executable() -> None:
    for rel in (
        "bin/delivery-loop",
        "hooks/pipeline_stop.py",
        "hooks/pipeline_guard.py",
        "hooks/pipeline_prompt.py",
    ):
        assert os.access(ROOT / rel, os.X_OK), rel


def test_the_slash_command_passes_its_words_where_no_shell_reads_them() -> None:
    # Value: protects=a task title with an apostrophe or $(...) starts a run as typed;
    # fails_when=$ARGUMENTS is spliced into a shell line; why_new=review QA probe; seam=none
    text = (ROOT / "commands/pipeline.md").read_text(encoding="utf-8")
    front = text.split("---")[1]
    assert re.search(r"^disable-model-invocation: true$", front, re.M)
    assert '--session "${CLAUDE_SESSION_ID}" --args-stdin' in text
    assert "<<'DELIVERY_LOOP_ARGS'\n$ARGUMENTS\nDELIVERY_LOOP_ARGS" in text
    assert text.count("$ARGUMENTS") == 2  # the heredoc, and the prose describing it


def test_every_default_stage_has_a_prompt_in_the_plugin() -> None:
    from core.config import DEFAULTS

    for stage in DEFAULTS.stages:
        assert (ROOT / "skills/pipeline/stages" / stage.prompt).is_file(), stage.name


def test_the_stage_prompts_write_no_slash_command() -> None:
    # Value: protects=prompts stay harness neutral; the stage's command key names the command;
    # fails_when=a prompt hard-codes a slash command; why_new=prompts moved here; seam=none
    for path in (ROOT / "skills/pipeline/stages").glob("*.md"):
        assert not re.search(r"(?<![\w/])/[a-z][\w-]+", path.read_text(encoding="utf-8")), path
