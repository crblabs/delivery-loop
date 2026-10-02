"""Render the pull request `## Decisions` section from logged decisions.

A run decides every reversible choice itself, logs each one, and prints them in
the pull request body under `## Decisions`, so the operator sees what the run
settled without being asked. This is the render half: it reads the decision
records as a JSON array on standard input (what `gstack-decision-search --json`
prints) and writes the markdown block on standard output.

    gstack-decision-search --scope branch --json | loop-pr-decisions

Keeping the render in a tool, not in the model's head, makes it deterministic
and testable without gstack installed. One line per decision, the choice and
its reason. An empty array, or an empty stream, renders the literal `None`, so a
run that decided nothing still carries a well-formed section.

Input: a JSON array on stdin (or a file path as the one argument). Each element
is an object with a `decision` field and an optional `rationale`. Exit codes
follow the shared tools contract: 0 clean, 2 usage or environment (input that is
not a JSON array of objects, unreadable or non-UTF-8 file, bad usage).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HEADING = "## Decisions"


def _one_line(record: dict) -> str:
    """One decision as one bullet: the choice, then its reason when present.
    Whitespace is collapsed so a multi-line rationale stays on one line."""
    decision = " ".join(str(record.get("decision", "")).split()).strip()
    rationale = " ".join(str(record.get("rationale", "")).split()).strip()
    if not decision:
        decision = "(unnamed decision)"
    if rationale:
        return f"- {decision}. Why: {rationale}"
    return f"- {decision}"


def render(records: list) -> str:
    """The `## Decisions` block for a list of decision records. `None` when the
    list is empty."""
    lines = [HEADING]
    if not records:
        lines.append("None")
    else:
        lines.extend(_one_line(r if isinstance(r, dict) else {}) for r in records)
    return "\n".join(lines) + "\n"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loop-pr-decisions",
        description="Render the pull request ## Decisions section from a JSON array.",
    )
    parser.add_argument("decisions", nargs="?", default=None, help="JSON file (default stdin)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    if args.decisions is not None:
        try:
            raw = Path(args.decisions).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"error: cannot read {args.decisions}: {exc}", file=sys.stderr)
            return 2
    else:
        raw = sys.stdin.read()

    # An empty stream is an empty decision set, not an error: a stage that
    # logged nothing pipes nothing, and the section should still render `None`.
    if not raw.strip():
        records: list = []
    else:
        try:
            records = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"error: input is not valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(records, list):
            print("error: input must be a JSON array of decision objects", file=sys.stderr)
            return 2
        if not all(isinstance(r, dict) for r in records):
            print("error: each array element must be a JSON object", file=sys.stderr)
            return 2

    sys.stdout.write(render(records))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
