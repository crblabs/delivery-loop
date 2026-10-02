"""The supervisor skill calls only commands the plugin puts on the agent's PATH.

The routine's command blocks are code the agent runs as written, so every
``loop-*`` name in them must be a launcher in ``bin/`` and a console entry point
in ``[project.scripts]``.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "supervisor"
_FENCE_RE = re.compile(r"^```[^\n]*\n(.*?)^```", re.M | re.S)
_COMMAND_RE = re.compile(r"(?<![\w./-])loop-[a-z][a-z-]*[a-z]")


def _commands_in_code_blocks() -> set[str]:
    names: set[str] = set()
    for path in sorted(SKILL.glob("*.md")):
        for block in _FENCE_RE.findall(path.read_text(encoding="utf-8")):
            names.update(_COMMAND_RE.findall(block))
    return names


def _scripts() -> dict[str, str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["scripts"]


def test_the_skill_has_a_name_and_a_description() -> None:
    front = (SKILL / "SKILL.md").read_text(encoding="utf-8").split("---")[1]
    assert re.search(r"^name: supervisor$", front, re.M)
    assert re.search(r"^description: \S", front, re.M)


def test_every_command_the_routine_calls_is_on_the_path_and_declared() -> None:
    # Value: protects=the supervisor runs its routine as written from any checkout;
    # fails_when=a code block names a loop-* command with no bin/ launcher or no entry
    # point; why_new=the skill moved into the plugin; seam=PATH
    names = _commands_in_code_blocks()
    assert {"loop-scan", "loop-transcript", "loop-decide", "loop-card"} <= names
    scripts = _scripts()
    for name in sorted(names):
        assert (ROOT / "bin" / name).is_file(), f"bin/{name} is missing"
        assert name in scripts, f"{name} is not in [project.scripts]"


@pytest.mark.parametrize("name", sorted(_commands_in_code_blocks()))
def test_each_launcher_parses_on_3_8_and_finds_its_module(name: str) -> None:
    # Value: protects=a launcher reaches the module its entry point names;
    # fails_when=a launcher imports the wrong module or a 3.9+ construct lands;
    # why_new=new launchers; seam=PATH
    launcher = ROOT / "bin" / name
    tree = ast.parse(launcher.read_text(encoding="utf-8"), feature_version=(3, 8))
    imported = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert imported == [_scripts()[name].split(":")[0]]
    done = subprocess.run(
        [sys.executable, str(launcher), "--help"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith(f"usage: {name}")
