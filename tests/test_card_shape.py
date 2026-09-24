"""The decision-card shape lint, pinned by the cards it must accept and refuse.

A pause reaches a person as a card, and the checker is what keeps that shape
honest. Each test names the failure it prevents: a missing label, labels out of
order, a recommendation that is not option 1, a reply token count that does not
match the options, a card over the word budget, and prose after the card that
would push it out of the truncated tail. The command-line tests pin the exit
codes: 0 clean, 1 refused, 2 on a usage or input error.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from core import card_shape as cs

TOOL = Path(__file__).resolve().parent.parent / "core" / "card_shape.py"


def _well_formed_question() -> str:
    return "\n".join(
        [
            "The metric choice changes what the report shows, so it stops here.",
            "",
            "ASK   Count statements from the syntax tree, or take radon's count?",
            "REC   1. Syntax tree: stricter, matches the plan",
            "ALT   2. Radon: looser, off the shelf",
            "COST  1 adds a parser pass. 2 undercounts multi-line calls.",
            "REPLY 1 | 2",
        ]
    )


def test_needs_human_well_formed_passes() -> None:
    assert cs.needs_human_shape_violations(_well_formed_question()) == []


def test_needs_human_missing_label_is_flagged() -> None:
    text = _well_formed_question().replace("ALT   2. Radon: looser, off the shelf\n", "")
    violations = cs.needs_human_shape_violations(text)
    assert any("ALT" in v for v in violations)


def test_needs_human_wrong_order_is_flagged() -> None:
    text = "\n".join(["REC   1. X", "ASK   X or Y?", "ALT   2. Y", "REPLY 1 | 2"])
    violations = cs.needs_human_shape_violations(text)
    assert any("order" in v.lower() for v in violations)


def test_needs_human_recommendation_must_be_option_one() -> None:
    text = _well_formed_question().replace("REC   1. Syntax tree", "REC   Syntax tree")
    violations = cs.needs_human_shape_violations(text)
    assert any("1." in v for v in violations)


def test_needs_human_reply_token_per_option() -> None:
    text = _well_formed_question().replace("REPLY 1 | 2", "REPLY 1")
    violations = cs.needs_human_shape_violations(text)
    assert any("REPLY" in v for v in violations)


def test_needs_human_word_budget_is_enforced() -> None:
    text = _well_formed_question().replace("ASK   ", "ASK   " + "word " * 60)
    violations = cs.needs_human_shape_violations(text)
    assert any("budget" in v for v in violations)


def test_needs_human_card_must_be_last() -> None:
    """The hook keeps the last 500 characters of the message, so prose after the
    card would push it out of the state file."""
    text = _well_formed_question() + "\nOne more paragraph.\n<promise>NEEDS HUMAN</promise>"
    violations = cs.needs_human_shape_violations(text)
    assert any("last block" in v for v in violations)
    ok = _well_formed_question() + "\n\n*<promise>NEEDS HUMAN</promise>*\n"
    assert cs.needs_human_shape_violations(ok) == []


def test_needs_human_cli_pass_and_fail(tmp_path: Path) -> None:
    good = tmp_path / "good.txt"
    good.write_text(_well_formed_question(), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(TOOL), str(good)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr

    bad = tmp_path / "bad.txt"
    bad.write_text("Effect for you: X or Y.\nOptions:\n- a\n- b\n", encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(TOOL), str(bad)],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "shape" in proc.stderr.lower()


def test_needs_human_cli_empty_is_usage_error() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOL)],
        input="  \n",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


def test_needs_human_prose_may_repeat_a_label_word() -> None:
    """The card is the LAST block, so a sentence above it that starts with a
    label word (`ALT text` in prose) does not start the card early."""
    text = "ASK yourself first: this is prose.\n\n" + _well_formed_question()
    assert cs.needs_human_shape_violations(text) == []


def test_needs_human_empty_label_is_flagged() -> None:
    text = _well_formed_question().replace(
        "COST  1 adds a parser pass. 2 undercounts multi-line calls.", "COST"
    )
    violations = cs.needs_human_shape_violations(text)
    assert any("COST" in v and "empty" in v for v in violations)


def test_needs_human_option_numbers_must_run_from_one() -> None:
    text = _well_formed_question().replace("ALT   2. Radon", "ALT   3. Radon")
    violations = cs.needs_human_shape_violations(text)
    assert any("numbered 1..n" in v for v in violations)


def test_needs_human_word_replies_are_allowed() -> None:
    """A guard or failure card replies with words, one per option."""
    text = "\n".join(
        [
            "ASK   Loop files changed: qa.md. Accept?",
            "REC   1. accept",
            "ALT   2. /pipeline abort",
            "REPLY accept | abort",
        ]
    )
    assert cs.needs_human_shape_violations(text) == []
