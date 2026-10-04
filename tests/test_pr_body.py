"""The pull request body section check.

A body that omits a required section must fail, and the complete body must pass.
The rest guard the false positives: a heading inside a fenced code block or an
HTML comment, or at the wrong level, must not count as the section being present.
Input errors (empty body, unreadable file) exit 2, distinct from a missing
section. The sections come from the host, so the tests name their own.
"""

from __future__ import annotations

import ast
import io
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from core import pr_body as pb

PLUGIN = Path(__file__).resolve().parent.parent
SECTIONS = ["Effect", "Picture", "Shape", "Changed", "Verified", "Findings", "Questions asked"]
FLAG = ",".join(SECTIONS)


def _complete_body() -> str:
    """Every section, an optional one, and a fenced JSON block, i.e. what a
    real pull request looks like."""
    return "\n".join(
        [
            "## Effect",
            "Adds the review-section gate.",
            "## Picture",
            "```mermaid",
            "graph TD; A-->B;",
            "```",
            "## Shape",
            "6 files, +200/-20. Risk class: ci.",
            "## Evidence",
            "tests/test_pr_body.py::test_missing_effect_is_refused",
            "## Changed",
            "- the checker and its workflow",
            "## Verified",
            "make check green",
            "## Findings",
            "- make pr-body a required check",
            "## Questions asked",
            "- CI wiring: separate workflow (approved)",
            "```json",
            '{"pipeline_run": {"task": "demo"}}',
            "```",
        ]
    )


# ------------------------------------------------------------- the parser


def test_complete_body_has_no_missing_sections() -> None:
    assert pb.missing_sections(_complete_body(), SECTIONS) == []


def test_missing_effect_is_refused() -> None:
    body = _complete_body().replace("## Effect\n", "")
    assert pb.missing_sections(body, SECTIONS) == ["Effect"]


def test_a_section_the_host_does_not_require_may_be_absent() -> None:
    body = _complete_body().replace(
        "## Evidence\ntests/test_pr_body.py::test_missing_effect_is_refused\n", ""
    )
    assert pb.missing_sections(body, SECTIONS) == []


def test_heading_inside_fenced_block_does_not_count() -> None:
    body = _complete_body().replace(
        "## Shape\n6 files, +200/-20. Risk class: ci.\n",
        "```\n## Shape\n```\n",
    )
    assert "Shape" in pb.missing_sections(body, SECTIONS)


def test_heading_inside_tilde_fence_does_not_count() -> None:
    body = _complete_body().replace(
        "## Findings\n- make pr-body a required check\n",
        "~~~\n## Findings\n~~~\n",
    )
    assert "Findings" in pb.missing_sections(body, SECTIONS)


def test_heading_inside_html_comment_does_not_count() -> None:
    """A template can fold a checklist that names the sections into a comment."""
    body = _complete_body().replace(
        "## Verified\nmake check green\n",
        "<!--\n## Verified\nreviewer: trace the main path\n-->\n",
    )
    assert "Verified" in pb.missing_sections(body, SECTIONS)


def test_wrong_heading_level_does_not_count() -> None:
    body = _complete_body().replace("## Changed", "### Changed")
    assert "Changed" in pb.missing_sections(body, SECTIONS)
    body = _complete_body().replace("## Changed", "# Changed")
    assert "Changed" in pb.missing_sections(body, SECTIONS)


def test_extra_heading_text_does_not_count() -> None:
    body = _complete_body().replace("## Effect", "## Effect (one sentence)")
    assert "Effect" in pb.missing_sections(body, SECTIONS)


def test_case_and_crlf_are_tolerated() -> None:
    """`gh pr view` can hand back CRLF, and heading case must not matter."""
    body = _complete_body().replace("## Effect", "## effect").replace("\n", "\r\n")
    assert pb.missing_sections(body, SECTIONS) == []


def test_all_missing_lists_every_section_in_order() -> None:
    assert pb.missing_sections("nothing here", SECTIONS) == SECTIONS


def test_closing_fence_with_info_string_does_not_close() -> None:
    """A ```python line is not a closing fence, so a heading trapped after it
    inside an open ``` block stays hidden (a false closer would leak it)."""
    body = _complete_body().replace(
        "## Shape\n6 files, +200/-20. Risk class: ci.\n",
        "```\ncode\n```python\n## Shape\n```\n",
    )
    assert "Shape" in pb.missing_sections(body, SECTIONS)


def test_indented_code_heading_does_not_count() -> None:
    body = _complete_body().replace("## Changed", "    ## Changed")
    assert "Changed" in pb.missing_sections(body, SECTIONS)


def test_three_space_indent_still_counts() -> None:
    body = _complete_body().replace("## Effect", "   ## Effect")
    assert pb.missing_sections(body, SECTIONS) == []


def test_unclosed_html_comment_hides_headings() -> None:
    body = _complete_body().replace("## Findings", "<!-- open\n## Findings")
    assert "Findings" in pb.missing_sections(body, SECTIONS)


# ------------------------------------------------------------- the sections


def test_the_flag_splits_on_commas_and_drops_blanks() -> None:
    assert pb.parse_sections_flag(" Effect , Questions asked ,,") == ["Effect", "Questions asked"]


def test_the_file_skips_blanks_and_comments() -> None:
    text = "# the sections we review\nEffect\n\n  # indented comment\nQuestions asked\n"
    assert pb.parse_sections_file(text) == ["Effect", "Questions asked"]


def test_the_flag_wins_over_the_file(tmp_path: Path) -> None:
    (tmp_path / pb.SECTIONS_PATH).write_text("Effect\n", encoding="utf-8")
    assert pb.load_sections("Shape", tmp_path) == ["Shape"]
    assert pb.load_sections(None, tmp_path) == ["Effect"]


def test_the_file_is_read_at_the_repository_root(
    make_repo: Callable[[Path], Path], tmp_path: Path
) -> None:
    root = make_repo(tmp_path / "host")
    (root / pb.SECTIONS_PATH).write_text("Effect\n", encoding="utf-8")
    sub = root / "docs"
    sub.mkdir()
    assert pb.load_sections(None, sub) == ["Effect"]


def test_an_unreadable_file_is_a_fault(tmp_path: Path) -> None:
    (tmp_path / pb.SECTIONS_PATH).write_bytes(b"\xff\xfe not utf-8")
    with pytest.raises(pb.BodyError):
        pb.load_sections(None, tmp_path)


# ------------------------------------------------------------- main


def _stdin(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(text))


def test_main_complete_body_via_stdin_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    _stdin(monkeypatch, _complete_body())
    assert pb.main(["--sections", FLAG]) == 0


def test_main_missing_section_via_file_exits_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body_file = tmp_path / "body.md"
    body_file.write_text(_complete_body().replace("## Effect\n", ""), encoding="utf-8")
    assert pb.main(["--sections", FLAG, str(body_file)]) == 1
    assert "## Effect" in capsys.readouterr().err


def test_main_reads_the_sections_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # The autouse fixture puts the test in an empty directory, not a repository.
    Path(pb.SECTIONS_PATH).write_text("Effect\nRollback\n", encoding="utf-8")
    _stdin(monkeypatch, _complete_body())
    assert pb.main([]) == 1
    assert "## Rollback" in capsys.readouterr().err


def test_main_with_no_sections_passes_and_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stdin(monkeypatch, "anything")
    assert pb.main([]) == 0
    assert "no pull request sections are configured" in capsys.readouterr().out


def test_main_empty_body_exits_two(monkeypatch: pytest.MonkeyPatch) -> None:
    _stdin(monkeypatch, "   \n")
    assert pb.main(["--sections", FLAG]) == 2


def test_main_unreadable_file_exits_two(tmp_path: Path) -> None:
    assert pb.main(["--sections", FLAG, str(tmp_path / "does-not-exist.md")]) == 2


def test_main_non_utf8_file_exits_two(tmp_path: Path) -> None:
    bad = tmp_path / "bad.md"
    bad.write_bytes(b"\xff\xfe not valid utf-8")
    assert pb.main(["--sections", FLAG, str(bad)]) == 2


# ------------------------------------------------------------- the launcher


def test_the_launcher_parses_on_3_8() -> None:
    ast.parse((PLUGIN / "bin" / "loop-pr-body").read_text(encoding="utf-8"), feature_version=(3, 8))


def test_the_launcher_runs_the_check(tmp_path: Path) -> None:
    # Value: protects=the ship stage calls the check by name from any host repo;
    # fails_when=bin/loop-pr-body cannot find core; why_new=the ship stage runs it; seam=PATH
    launcher = PLUGIN / "bin" / "loop-pr-body"
    run = subprocess.run(
        [launcher, "--sections", FLAG],
        cwd=tmp_path,
        input=_complete_body().replace("## Effect\n", ""),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1
    assert "## Effect" in run.stderr


def test_the_ship_stage_runs_the_check() -> None:
    text = (PLUGIN / "skills" / "pipeline" / "stages" / "ship.md").read_text("utf-8")
    assert "loop-pr-body <" in text
