"""Every pause reaches the operator as a decision card.

The fixtures are one real ship pause of 2026-09-22 08:30, twice: as the run
wrote it in prose (the last 500 characters the hook kept), and as a stage prompt
writes it once the card shape landed (the card last, so the tail keeps it
whole). Both must render as a card under the word budget with a one-token
reply. The other pause shapes the supervisor synthesises are pinned here too,
and the card a run writes must satisfy the same checker the stage prompts
self-check with.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core import card_shape as cs
from core import supervisor_card as sc

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "supervisor"
TOOL = Path(__file__).resolve().parent.parent / "core" / "supervisor_card.py"


def _record(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))["record"]


def _body_words(card: str) -> int:
    return sum(len(line[5:].split()) for line in card.split("\n")[1:])


def _labels(card: str) -> list[str]:
    return [line.split()[0] for line in card.split("\n")[1:]]


def test_prose_pause_renders_as_a_card() -> None:
    """The 300-word prose question becomes a card with the three options
    numbered, the recommended one first, and a one-digit reply."""
    card = sc.render(_record("pause-prose.json"))
    lines = card.split("\n")
    assert lines[0].startswith(
        "TASK-14 | ship | paused 2026-09-22 08:30 | Emdash session 8aff523e (cod-14)"
    )
    assert _labels(card) == ["ASK", "REC", "ALT", "REPLY"]
    assert "REC   1. Rework TASK-14 onto TASK-11's landed schema" in card
    assert "2. Keep TASK-14's event-log design" in card
    assert "3. Pause TASK-14" in card
    assert lines[-1] == "REPLY 1 | 2 | 3"
    assert _body_words(card) <= sc.WORD_BUDGET


def test_card_pause_passes_through_whole() -> None:
    """A card the run wrote is kept word for word under a fresh header, and it
    satisfies the checker the stage prompts self-check with."""
    fixture = json.loads((FIXTURES / "pause-card.json").read_text(encoding="utf-8"))
    assert cs.needs_human_shape_violations(fixture["full_message"]) == []
    card = sc.render(fixture["record"])
    assert _labels(card) == ["ASK", "REC", "ALT", "COST", "REPLY"]
    assert "ASK   TASK-11 landed the same match-log table on main with another schema." in card
    assert card.endswith("REPLY 1 | 2 | 3")
    assert _body_words(card) <= sc.WORD_BUDGET


def test_first_line_is_header_plus_ask_within_the_alert_limit() -> None:
    card = sc.render(_record("pause-card.json"))
    line = sc.first_line(card)
    assert line.startswith("TASK-14 | ship | paused")
    assert "Which design stays?" in line
    assert "\n" not in line
    assert len(line) <= sc.FIRST_LINE_CHARS


def test_done_run_is_one_line() -> None:
    record = {"task": "TASK-13", "status": "done", "pr_url": "https://github.com/x/y/pull/144"}
    assert sc.render(record) == "TASK-13 | done | PR #144 | merge or close"


def _base(**over: object) -> dict:
    record = {
        "task": "TASK-9",
        "current_stage": "ship",
        "status": "awaiting_human",
        "session_id": "abcdef12-0000",
        "worktree": "/w/operator-cod-9-read",
        "updated_at": "2026-09-22T08:05:25Z",
        "is_stale": True,
        "age_seconds": 7200.0,
    }
    record.update(over)
    return record


def test_guard_change_card_names_the_files_and_replies_accept_or_abort() -> None:
    text = (
        "A loop guard file changed (.claude/skills/pipeline/stages/qa.md, .claude/hooks/x); paused."
    )
    card = sc.render(_base(paused_reason="guard_changed", pending_question=text))
    assert "ASK   Loop files changed: qa.md, x. Accept?" in card
    assert card.endswith("REPLY accept | abort")


def test_a_stale_paused_reason_does_not_outrank_a_live_run() -> None:
    """paused_reason survives a resume, so a running run with an old
    guard_changed reason gets the liveness card, not a pause card."""
    card = sc.render(_base(status="running", paused_reason="guard_changed", is_stale=False))
    assert "Nothing to decide" in card
    card = sc.render(_base(status="running", paused_reason="guard_changed", is_stale=True))
    assert "Run looks dead" in card
    assert card.endswith("REPLY resume | wait")


def test_failed_run_card_never_says_resume_is_automatic() -> None:
    card = sc.render(_base(status="failed", paused_reason="cap_total"))
    assert "ASK   Run failed (cap_total). Recover?" in card
    assert "1. Fix the cause, then /pipeline resume" in card
    assert card.endswith("REPLY resume | abort")


def test_ask_user_question_card_puts_the_recommended_option_first() -> None:
    question = {
        "outcome": "pending",
        "prompt": "What should this run build?",
        "options": [
            {"label": "Hold", "recommended": False},
            {"label": "Write the docs", "recommended": True},
            {"label": "Advance data", "recommended": False},
        ],
    }
    card = sc.render(_base(status="running", is_stale=False), question)
    assert "ASK   What should this run build?" in card
    assert "REC   1. Write the docs" in card
    assert "ALT   2. Hold   3. Advance data" in card
    assert card.endswith("REPLY 1 | 2 | 3")


def test_permission_prompt_card_names_the_command_even_on_a_done_run() -> None:
    question = {"outcome": "permission_prompt", "tool": "Bash", "command": "git push -u origin x"}
    card = sc.render(_base(status="done"), question)
    assert "ASK   Approve Bash: git push -u origin x?" in card
    assert card.endswith("REPLY Yes | No")


def test_promotion_card_only_when_the_run_asked_no_question_of_its_own() -> None:
    promo = ["data/reference/sources.csv@" + "a" * 40]
    card = sc.render(
        _base(
            paused_reason="needs_human",
            pending_promotion=promo,
            pending_question="PROMOTION: data/reference/sources.csv@" + "a" * 40,
        )
    )
    assert "ASK   Promote into data/: data/reference/sources.csv?" in card
    assert card.endswith("REPLY approve | reject")
    with_options = "Options:\n- keep (recommended): a\n- drop: b\n"
    card = sc.render(
        _base(paused_reason="needs_human", pending_promotion=promo, pending_question=with_options)
    )
    assert "REC   1. keep" in card


def test_every_synthesised_card_fits_the_budget() -> None:
    long_paths = ", ".join(f".claude/skills/pipeline/stages/{n}.md" for n in range(20))
    cards = [
        sc.render(
            _base(
                paused_reason="guard_changed", pending_question=f"changed ({long_paths}); paused."
            )
        ),
        sc.render(_base(status="failed", paused_reason="runaway")),
        sc.render(
            _base(
                pending_question="x " * 400
                + "\nOptions:\n- "
                + "word " * 80
                + "(recommended)\n- "
                + "word " * 80
            )
        ),
    ]
    for card in cards:
        assert _body_words(card) <= sc.WORD_BUDGET, card


def test_cli_renders_a_fixture_and_its_first_line(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--record-file", str(FIXTURES / "pause-card.json")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip().endswith("REPLY 1 | 2 | 3")
    proc = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--record-file",
            str(FIXTURES / "pause-card.json"),
            "--first-line",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
    assert proc.stdout.count("\n") == 1
    bad = tmp_path / "bad.json"
    bad.write_text("[1, 2]", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(TOOL), "--record-file", str(bad)], capture_output=True, text=True
    )
    assert proc.returncode == 2
