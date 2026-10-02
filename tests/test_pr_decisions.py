"""The `## Decisions` renderer.

The render must be deterministic and testable without gstack installed, so the
tests feed it decision arrays and read the block. The block it prints must be a
heading the body check counts, with decisions or with `None`.
"""

from __future__ import annotations

import ast
import io
import json
import subprocess
from pathlib import Path

import pytest

from core import pr_body as pb
from core import pr_decisions as pd

PLUGIN = Path(__file__).resolve().parent.parent


def _records() -> list[dict]:
    return [
        {"decision": "count from the syntax tree", "rationale": "the line count\nundercounts"},
        {"decision": "keep the flag name"},
    ]


def test_decisions_render_one_line_each() -> None:
    block = pd.render(_records())
    assert block == (
        "## Decisions\n"
        "- count from the syntax tree. Why: the line count undercounts\n"
        "- keep the flag name\n"
    )


def test_the_block_is_a_heading_the_body_check_counts() -> None:
    assert "decisions" in pb.present_headings(pd.render(_records()))


def test_empty_decision_set_still_renders_a_valid_section() -> None:
    block = pd.render([])
    assert block == "## Decisions\nNone\n"
    assert "decisions" in pb.present_headings(block)


def test_a_blank_decision_is_named() -> None:
    assert pd.render([{"rationale": "why"}]) == "## Decisions\n- (unnamed decision). Why: why\n"


# ------------------------------------------------------------- main


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def test_main_renders_stdin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, json.dumps(_records()))
    assert pd.main([]) == 0
    assert capsys.readouterr().out == pd.render(_records())


def test_main_empty_stream_renders_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, "  \n")
    assert pd.main([]) == 0
    assert capsys.readouterr().out == "## Decisions\nNone\n"


def test_main_reads_a_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "decisions.json"
    path.write_text(json.dumps(_records()), encoding="utf-8")
    assert pd.main([str(path)]) == 0
    assert "keep the flag name" in capsys.readouterr().out


@pytest.mark.parametrize("raw", ["not json", '{"decision": "x"}', '[null, 42, "lost"]'])
def test_main_rejects_input_that_is_not_an_array_of_objects(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    """A non-object element is a usage error, not a silent `(unnamed decision)`."""
    _stdin(monkeypatch, raw)
    assert pd.main([]) == 2


def test_main_unreadable_file_exits_two(tmp_path: Path) -> None:
    assert pd.main([str(tmp_path / "missing.json")]) == 2


# ------------------------------------------------------------- the launcher


def test_the_launcher_parses_on_3_8() -> None:
    text = (PLUGIN / "bin" / "loop-pr-decisions").read_text(encoding="utf-8")
    ast.parse(text, feature_version=(3, 8))


def test_the_launcher_renders_stdin(tmp_path: Path) -> None:
    # Value: protects=the ship stage pipes the decision log through it by name;
    # fails_when=bin/loop-pr-decisions cannot find core; why_new=the ship stage runs it; seam=PATH
    run = subprocess.run(
        [PLUGIN / "bin" / "loop-pr-decisions"],
        cwd=tmp_path,
        input="[]",
        capture_output=True,
        text=True,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "## Decisions\nNone\n"


def test_the_ship_stage_runs_the_renderer() -> None:
    text = (PLUGIN / "skills" / "pipeline" / "stages" / "ship.md").read_text("utf-8")
    assert "gstack-decision-search --scope branch --json | loop-pr-decisions" in text
