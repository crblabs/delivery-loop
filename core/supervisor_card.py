#!/usr/bin/env python3
"""Render one paused run as a decision card the operator answers in a minute.

The card is the one shape every pause takes on its way to a person: the
supervisor reply, the desktop alert (its first line), and the tracker comment.
It puts the decision first and numbers the options, so the reply is one digit.

    TASK-14 | ship | paused 2026-09-22 08:30 | session 8aff523e (task-14)
    ASK   Main landed the same match-log table with another schema. Which stays?
    REC   1. Rework this branch onto main's schema via the --record interface
    ALT   2. Keep this log, redo main   3. Refile the branch as a scorer
    COST  1 reworks this branch. 2 reworks main. 3 writes nothing now.
    REPLY 1 | 2 | 3

The session label, the operator commands a card quotes, and the shape of an
issue identifier are config fields, so no harness or tracker name is written
here.

A run that already wrote its question as a card (the delivery-loop stages do) is
passed through under a fresh header. Every other pause is synthesised from the
scan record and the pending gate: a guard change, a promotion, a failed run, a
stale run, a harness permission prompt, or an ``AskUserQuestion``. A ``done``
run is one line.

Inputs are the JSON a scan record and a question already carry.
``--first-line`` prints only the notification line. The tool never writes and
never touches the network.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

from core.config import DEFAULTS, LoopConfig, load_config

WORD_BUDGET = 60
FIRST_LINE_CHARS = 200
LABELS = ("ASK", "REC", "ALT", "COST", "REPLY")
_LABEL_RE = re.compile(r"^(ASK|REC|ALT|COST|REPLY)\b[ \t]*(.*)$")
_OPTION_RE = re.compile(r"^[ \t]*[-*+][ \t]+(.*)$")
_TOKEN = "<promise>NEEDS HUMAN</promise>"


def _short_session(session_id: object) -> str:
    return str(session_id or "?")[:8]


def _short_worktree(worktree: object, config: LoopConfig) -> str:
    """The task-identifier part of a worktree name, or the basename."""
    base = Path(str(worktree or "")).name
    m = config.tracker_re.search(base)
    return m.group(1) if m else (base or "?")


def _when(value: object) -> str:
    if not isinstance(value, str):
        return "?"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:16]
    return dt.strftime("%Y-%m-%d %H:%M")


def _words(text: str) -> int:
    return len(text.split())


def _cut(text: str, words: int) -> str:
    parts = " ".join(text.split()).split(" ")
    if len(parts) <= words:
        return " ".join(parts)
    return " ".join(parts[:words]).rstrip(",.;:") + " ..."


def _pr_number(url: object) -> str:
    m = re.search(r"/pull/(\d+)", str(url or ""))
    return f"PR #{m.group(1)}" if m else "no PR"


def header(record: dict, state_word: str, config: LoopConfig = DEFAULTS) -> str:
    task = record.get("task") or "?"
    stage = record.get("current_stage") or record.get("stage") or "?"
    where = (
        f"{config.session_label} {_short_session(record.get('session_id'))} "
        f"({_short_worktree(record.get('worktree'), config)})"
    )
    return f"{task} | {stage} | {state_word} {_when(record.get('updated_at'))} | {where}"


def _card_from_lines(lines: list[str]) -> dict[str, str]:
    card: dict[str, str] = {}
    current: str | None = None
    for ln in lines:
        if ln.strip() == _TOKEN:
            break
        m = _LABEL_RE.match(ln)
        if m:
            current = m.group(1)
            card[current] = m.group(2).strip()
        elif current and ln.strip():
            card[current] = f"{card[current]} {ln.strip()}".strip()
    return card


def extract_card(text: str) -> dict[str, str] | None:
    """The card a run wrote, as {label: content}, or None when the text has no
    ASK line. The last ASK wins, because the card sits last before the token."""
    lines = (text or "").split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith("ASK") and _LABEL_RE.match(ln)]
    if not starts:
        return None
    card = _card_from_lines(lines[starts[-1] :])
    return card if "ASK" in card else None


def _bullets_after(lines: list[str], start: int) -> list[str]:
    """Bullet lines from `start` until the first non-bullet, non-indented line."""
    found: list[str] = []
    for ln in lines[start:]:
        m = _OPTION_RE.match(ln)
        if m:
            found.append(m.group(1).strip())
        elif found and ln.strip() and not ln.startswith(" "):
            break
    return found


def _legacy_options(text: str) -> list[str]:
    """Bullet options under an `Options:` line in a prose question, the one
    marked `(recommended)` first, its marker removed."""
    lines = (text or "").split("\n")
    idx = next((i for i, ln in enumerate(lines) if ln.strip().lower().startswith("options")), None)
    if idx is None:
        return []
    found = _bullets_after(lines, idx + 1)
    rec = [o for o in found if "(recommended)" in o.lower()]
    rest = [o for o in found if "(recommended)" not in o.lower()]
    return [_option_head(o) for o in rec + rest]


def _option_head(option: str) -> str:
    """The outcome before the colon, marker removed: the prose shape put the
    mechanism after it, and the card only needs the outcome."""
    head = re.sub(r"\s*\(recommended\)", "", option, flags=re.I).strip()
    return head.split(":", 1)[0].strip() if ":" in head else head


def _ask_from_prose(text: str) -> str:
    """A question line from a prose tail. The 500-character tail usually starts
    mid-sentence, so a fragment yields a pointer instead of garbage."""
    first = text.strip().split("\n")[0].strip()
    if _words(first) < 4 or first[:1].islower():
        return "The run asks a question. Read it in the session."
    return _cut(first, 24)


def _numbered(options: list[str]) -> tuple[str, str, str]:
    """(REC, ALT, REPLY) for numbered options, the first being recommended."""
    if not options:
        return ("1. Read the question in the session", "", "reply in the session")
    per = min(12, max(4, (WORD_BUDGET - 12) // max(1, len(options))))
    rec = f"1. {_cut(options[0], per)}"
    alt = "   ".join(f"{n}. {_cut(o, per)}" for n, o in enumerate(options[1:], start=2))
    reply = " | ".join(str(n) for n in range(1, len(options) + 1))
    return rec, alt, reply


def _card(ask: str, rec: str, alt: str, cost: str, reply: str) -> dict[str, str]:
    return {"ASK": ask, "REC": rec, "ALT": alt, "COST": cost, "REPLY": reply}


def _recommended_first(options: list) -> list[str]:
    """Option labels with the recommended one moved to the front."""
    dicts = [o for o in options if isinstance(o, dict)]
    labels = [str(o.get("label", "")) for o in dicts]
    first = next((str(o.get("label", "")) for o in dicts if o.get("recommended")), None)
    return labels if first is None else [first, *[x for x in labels if x != first]]


def _question_card(q: dict) -> dict[str, str]:
    """An AskUserQuestion gate: its options numbered, the recommended one first."""
    rec, alt, reply = _numbered(_recommended_first(q.get("options") or []))
    return _card(_cut(q.get("prompt") or "The run asks a question.", 24), rec, alt, "", reply)


def _permission_card(q: dict) -> dict[str, str]:
    cmd = _cut(str(q.get("command") or ""), 12)
    return _card(
        f"Approve {q.get('tool') or 'tool'}: {cmd}?",
        "1. Yes, if the command is the run's own",
        "2. No",
        "1 runs it now. 2 stops the run there.",
        "Yes | No",
    )


def _failed_card(reason: object, config: LoopConfig) -> dict[str, str]:
    return _card(
        f"Run failed ({reason or 'unknown'}). Recover?",
        f"1. Fix the cause, then {config.resume_command}",
        f"2. {config.abort_command}",
        "1 retries the stage. 2 archives the run.",
        "resume | abort",
    )


def _guard_card(text: str, config: LoopConfig) -> dict[str, str]:
    m = re.search(r"\((.*?)\)", text)
    names = [Path(p.strip()).name for p in m.group(1).split(",")] if m else []
    paths = _cut(", ".join(n for n in names if n), 10) if names else "loop files"
    return _card(
        f"Loop files changed: {paths}. Accept?",
        "1. accept (main's version after a merge, or your own edit)",
        f"2. {config.abort_command}",
        "1 records the change and continues. 2 archives the run.",
        "accept | abort",
    )


def _promotion_card(promotion: list) -> dict[str, str]:
    paths = _cut(", ".join(str(p).split("@")[0] for p in promotion), 14)
    return _card(
        f"Promote into data/: {paths}?",
        "1. Read docs/confidentiality.md, then approve in the session",
        "2. Reject: reply so, the run keeps it in _staging",
        "1 publishes the rows. 2 holds them.",
        "approve | reject",
    )


def _paused_card(record: dict, config: LoopConfig) -> dict[str, str]:
    """A paused run: its own prose question, or the guard or promotion pause."""
    text = record.get("pending_question") or ""
    if record.get("paused_reason") == "guard_changed":
        return _guard_card(text, config)
    options = _legacy_options(text)
    # A stale pending_promotion from an earlier stage must not outrank the
    # question the run actually wrote, so the promotion card needs the run's
    # own PROMOTION lines or a question with no options of its own.
    promotion = record.get("pending_promotion") or []
    if promotion and ("PROMOTION:" in text or not options):
        return _promotion_card(promotion)
    rec, alt, reply = _numbered(options)
    return _card(_ask_from_prose(text), rec, alt, "", reply)


def _liveness_card(record: dict, config: LoopConfig) -> dict[str, str]:
    if not record.get("is_stale"):
        return _card("Nothing to decide: the run is live.", "1. Wait", "", "", "wait")
    age = record.get("age_seconds")
    hours = f"{float(age) / 3600:.0f}h" if isinstance(age, (int, float)) else "?"
    return _card(
        f"Run looks dead: running, no hook for {hours}. Adopt?",
        f"1. {config.resume_command} from a new session in the worktree",
        "2. Wait",
        "1 adopts the run. 2 leaves it.",
        "resume | wait",
    )


def _synthesise(record: dict, question: dict | None, config: LoopConfig) -> dict[str, str]:
    """A card for a pause the run did not write as one. A gate in the transcript
    outranks the state file, and paused_reason outlives the pause it named, so
    only a run paused now gets a pause card."""
    q = question if isinstance(question, dict) else {}
    status = record.get("status")
    if q.get("outcome") == "pending":
        return _question_card(q)
    if q.get("outcome") == "permission_prompt":
        return _permission_card(q)
    if status == "failed":
        return _failed_card(record.get("paused_reason"), config)
    if status == "awaiting_human":
        return _paused_card(record, config)
    return _liveness_card(record, config)


def _fit(card: dict[str, str]) -> dict[str, str]:
    """Trim ASK, then COST, until the card is within the word budget."""
    fitted = dict(card)
    for label in ("COST", "ASK"):
        while _words(" ".join(fitted.values())) > WORD_BUDGET and _words(fitted.get(label, "")) > 4:
            fitted[label] = _cut(fitted[label], _words(fitted[label]) - 4)
    return fitted


def _state_word(record: dict) -> str:
    live = "stale" if record.get("is_stale") else "running"
    return {"awaiting_human": "paused", "failed": "failed"}.get(str(record.get("status")), live)


def _body_lines(card: dict[str, str]) -> list[str]:
    """One line per label. COST and ALT are dropped when empty; the others stay."""
    lines = []
    for label in LABELS:
        text = card.get(label, "")
        if text or label in ("ASK", "REC", "REPLY"):
            lines.append(f"{label:<5} {text}".rstrip())
    return lines


def render(record: dict, question: dict | None = None, config: LoopConfig = DEFAULTS) -> str:
    """The card for one run, header first. A done run is one line, unless a
    permission prompt is parked on it."""
    prompt = isinstance(question, dict) and question.get("outcome") == "permission_prompt"
    if record.get("status") == "done" and not prompt:
        task, pr = record.get("task") or "?", _pr_number(record.get("pr_url"))
        return f"{task} | done | {pr} | merge or close"
    card = extract_card(record.get("pending_question") or "") or _synthesise(
        record, question, config
    )
    return "\n".join([header(record, _state_word(record), config), *_body_lines(_fit(card))])


def first_line(card: str) -> str:
    """The notification: header plus ASK, within the desktop alert's limit."""
    lines = card.split("\n")
    text = lines[0] if len(lines) == 1 else f"{lines[0]} :: {lines[1][5:].strip()}"
    return text if len(text) <= FIRST_LINE_CHARS else text[: FIRST_LINE_CHARS - 4] + " ..."


def _load(path: str) -> dict | None:
    if path == "-":
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render one run's pause as a decision card.")
    parser.add_argument("--record-file", required=True, help="one supervisor_scan.py record")
    parser.add_argument(
        "--question-file", default="-", help="a supervisor_transcript.py result, or -"
    )
    parser.add_argument(
        "--first-line", action="store_true", help="print only the notification line"
    )
    parser.add_argument("--config", default=None, help="a loop.toml, or the directory holding one")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    try:
        record = _load(args.record_file)
        question = _load(args.question_file)
    except (OSError, ValueError) as exc:
        print(f"supervisor_card: cannot read input: {exc}", file=sys.stderr)
        return 2
    if isinstance(record, dict) and "record" in record and "status" not in record:
        record = record["record"]  # a test fixture wraps the record beside a comment
    if not isinstance(record, dict):
        print("supervisor_card: the record must be one JSON object", file=sys.stderr)
        return 2
    card = render(record, question, config)
    print(first_line(card) if args.first_line else card)
    return 0


if __name__ == "__main__":
    sys.exit(main())
