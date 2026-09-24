#!/usr/bin/env python3
"""Check that a decision card carries the shape a person can answer in a minute.

A card is how one pause reaches a human. It is five line-leading labels in this
order: ASK, REC, ALT, COST, REPLY. ASK, REC, ALT and REPLY are required and COST
is optional. The options are numbered so the recommended one is option 1 and the
numbers run 1 to n. REPLY lists one token per option, each a single word or
digit. The whole card stays under the word budget, and it is the last block
before the pause token, so a truncated tail still keeps it whole.

The checker and the renderer are one contract. `core/supervisor_card.py` writes
cards in this shape and this module refuses cards that are not in it, so the two
must move together. A change to one without the other splits the contract.

Use it as a library through `needs_human_shape_violations`, or from the command
line to lint one card file. Command line: a file path as the one argument, or
the card on standard input. Exit codes are 0 clean and silent, 1 refused with
each violation on standard error, 2 on a usage or input error.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# A pause reaches a person as a card: five line-leading labels in this order,
# the options numbered so the reply is one digit, under the word budget, and
# last in the turn so the hook's 500-character tail keeps it whole.
CARD_LABELS = ("ASK", "REC", "ALT", "COST", "REPLY")
CARD_REQUIRED = ("ASK", "REC", "ALT", "REPLY")
CARD_WORD_BUDGET = 60
_CARD_LINE_RE = re.compile(r"^[ \t]*(?P<label>ASK|REC|ALT|COST|REPLY)\b[ \t]*(?P<content>.*)$")
_OPTION_NUMBER_RE = re.compile(r"(?<![\w.])(\d)\.[ \t]")
_TOKEN_LINE_RE = re.compile(r"^[ \t*_]*<promise>NEEDS HUMAN</promise>[ \t*_]*$")


def _label_of(line: str) -> str | None:
    m = _CARD_LINE_RE.match(line)
    return m.group("label") if m else None


def _card_lines(lines: list[str]) -> dict[str, int]:
    """The first line index of each card label. The card sits last, so the LAST
    ASK line starts it and earlier prose may repeat a word like ASK freely."""
    starts = [i for i, ln in enumerate(lines) if _label_of(ln) == "ASK"]
    found: dict[str, int] = {}
    for i in range(starts[-1] if starts else len(lines), len(lines)):
        label = _label_of(lines[i])
        if label and label not in found:
            found[label] = i
    return found


def _card_content(lines: list[str], idx: dict[str, int], label: str) -> str:
    """A label's content: the rest of its line plus continuation lines up to the
    next label or the token. REPLY is one line; what follows it is spill."""
    start = idx[label]
    later = [i for i in idx.values() if i > start]
    end = min(later) if later else len(lines)
    body = [_CARD_LINE_RE.match(lines[start]).group("content")]  # type: ignore[union-attr]
    if label != "REPLY":
        rest = lines[start + 1 : end]
        stop = next((k for k, ln in enumerate(rest) if _TOKEN_LINE_RE.match(ln)), len(rest))
        body.extend(rest[:stop])
    return " ".join(" ".join(body).split())


def _order_problems(idx: dict[str, int]) -> list[str]:
    missing = [p for p in CARD_REQUIRED if p not in idx]
    problems = ["missing label(s): " + ", ".join(missing)] if missing else []
    present = [p for p in CARD_LABELS if p in idx]
    for earlier, later in zip(present, present[1:], strict=False):
        if idx[earlier] > idx[later]:
            order = ", ".join(CARD_LABELS)
            problems.append(f"{later} comes before {earlier}; keep the order {order}")
            break
    return problems


def _option_problems(content: dict[str, str]) -> tuple[list[str], list[int]]:
    """Option numbering: REC is option 1 and the numbers run 1..n."""
    problems = []
    if content.get("REC") and not content["REC"].startswith("1."):
        problems.append("REC must start with '1.': the recommended option is always option 1")
    numbers = [
        int(n)
        for n in _OPTION_NUMBER_RE.findall(f"{content.get('REC', '')} {content.get('ALT', '')}")
    ]
    if numbers and numbers != list(range(1, len(numbers) + 1)):
        problems.append(f"options must be numbered 1..n in order, got {numbers}")
    return problems, numbers


def _reply_problems(reply: str, numbers: list[int]) -> list[str]:
    """One reply token per option, each a single word or digit."""
    if not reply:
        return []
    tokens = [tok.strip() for tok in reply.split("|")]
    problems = []
    if numbers and len(tokens) != len(numbers):
        problems.append(
            f"REPLY lists {len(tokens)} token(s) for {len(numbers)} option(s); one per option"
        )
    if any(len(tok.split()) != 1 for tok in tokens):
        problems.append("each REPLY token is one word or digit, separated by '|'")
    return problems


def _spill_after_card(lines: list[str], idx: dict[str, int]) -> bool:
    """Whether any prose follows the REPLY line other than the token."""
    if "REPLY" not in idx:
        return False
    tail = lines[idx["REPLY"] + 1 :]
    return any(ln.strip() and not _TOKEN_LINE_RE.match(ln) for ln in tail)


def needs_human_shape_violations(text: str) -> list[str]:
    """The ways `text` fails the decision-card shape, in reading order. Empty
    means the card holds: ASK, REC, ALT, REPLY present (COST optional), in
    order, each with content, options numbered 1..n with one reply token per
    option, under the word budget, and nothing but the token after REPLY."""
    lines = text.split("\n")
    idx = _card_lines(lines)
    problems = _order_problems(idx)
    content = {p: _card_content(lines, idx, p) for p in CARD_LABELS if p in idx}
    problems.extend(f"{p} is empty" for p, v in content.items() if not v)
    option_problems, numbers = _option_problems(content)
    problems.extend(option_problems)
    problems.extend(_reply_problems(content.get("REPLY", ""), numbers))
    words = sum(len(v.split()) for v in content.values())
    if words > CARD_WORD_BUDGET:
        problems.append(f"the card has {words} words; the budget is {CARD_WORD_BUDGET}")
    if _spill_after_card(lines, idx):
        problems.append("the card must be the last block before the token; move prose above it")
    return problems


def _read_input(args: list[str]) -> tuple[str | None, int]:
    """The card text from a file path in `args` or from stdin. Returns
    (text, 0) or (None, exit_code) on an input error."""
    if len(args) > 1:
        print("usage: card_shape.py [CARD_FILE]", file=sys.stderr)
        return None, 2
    if args:
        path = Path(args[0])
        try:
            return path.read_text(encoding="utf-8"), 0
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: cannot read {path}: {exc}", file=sys.stderr)
            return None, 2
    return sys.stdin.read(), 0


def main(argv: list[str]) -> int:
    text, code = _read_input(argv[1:])
    if text is None:
        return code
    if not text.strip():
        print("error: empty question text", file=sys.stderr)
        return 2
    violations = needs_human_shape_violations(text)
    if violations:
        print("NEEDS HUMAN question does not carry the required shape:", file=sys.stderr)
        for v in violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "A stop ends with a decision card: " + ", ".join(CARD_LABELS) + ".",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
