"""An issue draft, its lint rules, and the portfolio audit, for any tracker.

No agent writes the tracker directly. An agent writes a draft file, and
``loop-issue`` lints it; only a clean draft reaches the tracker. This module is
the tracker-neutral half: the draft format, the rules, the ``Tracker`` protocol
an adapter implements, and the audit with its ratchet. ``core.issue_writer`` is
the command, and ``adapters/`` holds one package per tracker.

THE DRAFT. One Markdown file: a front-matter block of ``key: value`` lines
between two ``---`` lines, then a body of ``##`` headed blocks. The keys are
``title``, ``team``, ``project``, ``milestone``, ``priority``, ``labels`` (comma
separated) and ``issue`` (the issue an update targets). A draft that names no
``team``, ``project`` or ``milestone`` inherits the one ``[tracker]`` sets in
``loop.toml``.

THE LINT RULES. A refusal is one line that names the rule and the fix.
  I1 Title.      Present, one line, no trailing period, at most 80 characters.
  I2 Blocks.     Effect, Now, Do, Done when, in that order, each non-empty.
                 Picture is optional and sits between Now and Do. A duplicate
                 or unknown heading is refused; a ``##`` in a fence is not one.
  I3 Words.      The body is at most 120 words. Fenced blocks and heading lines
                 do not count, so a Picture diagram is free.
  I4 No dash.    No banned dash in the title or the body, as
                 ``core.no_unicode_dash`` defines one. The hyphen is allowed.
  I5 Project.    Named; with a tracker connection, it must resolve.
  I6 Milestone.  Named; with a connection, it must resolve in the project.
  I7 Priority.   1 through 4.
  I8 Team, refs. A team is named (and resolves, with a connection), every
                 label resolves, and every token that starts like an issue id is
                 one whole: it must match ``[tracker] pattern`` from end to end.
I1 to I4, I7, the presence of the names and the references run offline.

THE AUDIT. Over every active issue and project the tracker lists:
  P1 Milestone coverage.  Active issues with no milestone.
  P2 Project coverage.    Active issues with no project.
  P3 Project updates.     Active projects with no update in the last 30 days.
  P4 Body length.         Active issue bodies over 120 words.
A ratchet baseline holds one count per rule, and the audit fails when a count
rises above it. The baseline moves only down. A read that fails is never scored.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from core.config import DEFAULTS, LoopConfig
from core.no_unicode_dash import dashes_in_text

TITLE_MAX = 80
WORD_LIMIT = 120
PRIORITY_RANGE = (1, 4)
REQUIRED_BLOCKS = ("Effect", "Now", "Do", "Done when")
BLOCK_ORDER = ("Effect", "Now", "Picture", "Do", "Done when")
FRONT_MATTER_KEYS = ("title", "team", "project", "milestone", "priority", "labels", "issue")

PROJECT_UPDATE_WINDOW_DAYS = 30
RULE_KEYS = ("P1", "P2", "P3", "P4")
BASELINE_SCHEMA = 1

_HEADING = re.compile(r"^##\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*```")
# Punctuation that may follow a reference in prose, such as a full stop.
_REF_TRAIL = ".,);:]"


class DraftError(Exception):
    """The draft or the baseline cannot be read, or the call is wrong (exit 2)."""


class TrackerError(Exception):
    """A tracker read or write failed, or its key is missing (exit 2)."""


# --- the tracker protocol ----------------------------------------------------


@dataclass(frozen=True)
class IssueInput:
    """What a write sends: the draft's text and the ids its names resolved to.

    ``None`` means the draft did not name it, so an update leaves it unchanged.
    """

    title: str
    body: str
    priority: int
    team_id: str | None = None
    project_id: str | None = None
    milestone_id: str | None = None
    label_ids: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Written:
    identifier: str
    url: str


@dataclass(frozen=True)
class AuditIssue:
    has_milestone: bool
    has_project: bool
    body: str


@dataclass(frozen=True)
class AuditProject:
    name: str
    last_update: datetime | None


class Tracker(Protocol):
    """The operations ``loop-issue`` needs from one tracker.

    A resolve returns the tracker's id for a name, or ``None`` when no such
    name exists. Every method raises ``TrackerError`` when a read or a write
    fails, so a partial answer is never taken for a whole one.
    """

    name: str

    def resolve_team(self, name: str) -> str | None: ...

    def resolve_project(self, name: str) -> str | None: ...

    def resolve_milestone(self, project_id: str, name: str) -> str | None: ...

    def resolve_labels(self, names: list[str]) -> dict[str, str | None]: ...

    def create_issue(self, issue: IssueInput) -> Written: ...

    def update_issue(self, reference: str, issue: IssueInput) -> Written: ...

    def active_issues(self) -> Iterable[AuditIssue]: ...

    def active_projects(self) -> Iterable[AuditProject]: ...


# --- the draft -----------------------------------------------------------------


@dataclass(frozen=True)
class Draft:
    fields: dict[str, str]
    body: str

    def get(self, key: str) -> str:
        return self.fields.get(key, "").strip()

    def labels(self) -> list[str]:
        return [n.strip() for n in self.get("labels").split(",") if n.strip()]


def parse_draft(text: str, config: LoopConfig = DEFAULTS) -> Draft:
    """The draft in ``text``, with the config's names filling what it leaves out."""
    fields, body = parse_front_matter(text)
    inherited = {
        "team": config.tracker_team,
        "project": config.tracker_project,
        "milestone": config.tracker_milestone,
    }
    for key, value in inherited.items():
        if not fields.get(key, "").strip() and value:
            fields[key] = value
    return Draft(fields, body)


def read_draft(path: Path, config: LoopConfig = DEFAULTS) -> Draft:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise DraftError(f"cannot read draft {path}: {exc}") from exc
    return parse_draft(text, config)


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Split a leading ``---`` block of ``key: value`` lines from the body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise DraftError("draft has no front matter: the first line must be `---`")
    fields: dict[str, str] = {}
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return fields, "\n".join(lines[i + 1 :]).strip("\n")
        if not line.strip():
            continue
        key, value = _front_matter_line(line)
        if key in fields:
            raise DraftError(f"front matter has a duplicate key: {key!r}")
        fields[key] = value
    raise DraftError("front matter is not closed: add a second `---` line")


def _front_matter_line(line: str) -> tuple[str, str]:
    if ":" not in line:
        raise DraftError(f"front matter line is not `key: value`: {line!r}")
    key, value = line.split(":", 1)
    key = key.strip()
    if key not in FRONT_MATTER_KEYS:
        raise DraftError(f"front matter key {key!r} is unknown; use {', '.join(FRONT_MATTER_KEYS)}")
    return key, value.strip()


def _outside_fences(body: str) -> list[str]:
    out: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
        elif not in_fence:
            out.append(line)
    return out


def headed_blocks(body: str) -> list[tuple[str, str]]:
    """The (heading, content) pairs. Content keeps its fences, so a Picture
    block that holds only a diagram is not read as empty."""
    blocks: list[tuple[str, list[str]]] = []
    in_fence = False
    for line in body.splitlines():
        heading = None if in_fence else _HEADING.match(line)
        if _FENCE.match(line):
            in_fence = not in_fence
        if heading:
            blocks.append((heading.group(1).strip(), []))
        elif blocks:
            blocks[-1][1].append(line)
    return [(head, "\n".join(rest).strip()) for head, rest in blocks]


def count_words(body: str) -> int:
    """Whitespace-separated words outside fenced blocks and heading lines."""
    return sum(len(line.split()) for line in _outside_fences(body) if not _HEADING.match(line))


# --- the offline rules ---------------------------------------------------------


def lint_title(draft: Draft) -> str | None:
    title = draft.get("title")
    if not title:
        return "I1 title: add a `title:` line to the front matter."
    if title.endswith("."):
        return "I1 title: remove the trailing period."
    if len(title) > TITLE_MAX:
        return f"I1 title: at most {TITLE_MAX} characters (it has {len(title)})."
    return None


def _blocks_named_problem(heads: list[str]) -> str | None:
    seen: set[str] = set()
    for head in heads:
        if head in seen:
            return f"I2 blocks: duplicate heading `{head}`."
        seen.add(head)
        if head not in BLOCK_ORDER:
            return f"I2 blocks: unexpected heading `{head}`; use {', '.join(BLOCK_ORDER)}."
    return None


def _blocks_content_problem(blocks: list[tuple[str, str]]) -> str | None:
    for head, content in blocks:
        if not content:
            return f"I2 blocks: the `{head}` block is empty."
    heads = [h for h, _ in blocks]
    for required in REQUIRED_BLOCKS:
        if required not in heads:
            return f"I2 blocks: the `{required}` block is missing."
    return None


def lint_blocks(body: str) -> str | None:
    blocks = headed_blocks(body)
    heads = [h for h, _ in blocks]
    problem = _blocks_named_problem(heads) or _blocks_content_problem(blocks)
    if problem:
        return problem
    if heads != sorted(heads, key=BLOCK_ORDER.index):
        return f"I2 blocks: order the blocks as {', '.join(BLOCK_ORDER)}."
    return None


def lint_words(body: str) -> str | None:
    words = count_words(body)
    if words > WORD_LIMIT:
        return f"I3 words: at most {WORD_LIMIT} words of prose (it has {words})."
    return None


def lint_dashes(draft: Draft) -> str | None:
    for where, text in (("title", draft.get("title")), ("body", draft.body)):
        if any(dashes_in_text(line) for line in text.splitlines()):
            return f"I4 dash: the {where} has a long dash; use a hyphen, a comma or a new sentence."
    return None


def lint_priority(draft: Draft) -> str | None:
    raw = draft.get("priority")
    low, high = PRIORITY_RANGE
    if not raw:
        return f"I7 priority: set `priority:` to {low} through {high}."
    if not raw.isdigit() or not low <= int(raw) <= high:
        return f"I7 priority: must be {low} through {high} (got {raw!r})."
    return None


def lint_present(draft: Draft) -> list[str]:
    missing = (("I5 project", "project"), ("I6 milestone", "milestone"), ("I8 team", "team"))
    return [
        f"{rule}: add a `{key}:` line, or set `{key}` under [tracker] in loop.toml."
        for rule, key in missing
        if not draft.get(key)
    ]


def reference_candidate_re(config: LoopConfig = DEFAULTS) -> re.Pattern[str]:
    """A token that starts like an issue id. With the default any-letters
    prefix it must have a digit after the dash, or every hyphenated word would
    be a candidate; a host that names its prefix gets the strict form."""
    tail = r"\d\S*" if config.tracker_prefix == DEFAULTS.tracker_prefix else r"\S+"
    return re.compile(rf"(?<![\w-])(?:{config.tracker_prefix})-{tail}")


def lint_references(body: str, config: LoopConfig = DEFAULTS) -> str | None:
    for token in reference_candidate_re(config).findall(body):
        candidate = token.rstrip(_REF_TRAIL)
        if not config.tracker_re.fullmatch(candidate):
            return (
                f"I8 reference: `{candidate}` is not a well-formed issue id; "
                f"it must match {config.tracker_pattern}."
            )
    return None


def lint_offline(draft: Draft, config: LoopConfig = DEFAULTS) -> list[str]:
    """Every offline refusal line. Empty means the shape is clean."""
    rules = (
        lint_title(draft),
        lint_blocks(draft.body),
        lint_words(draft.body),
        lint_dashes(draft),
        lint_priority(draft),
    )
    problems = [p for p in rules if p]
    problems += lint_present(draft)
    reference = lint_references(draft.body, config)
    return problems + ([reference] if reference else [])


# --- resolution through a tracker ------------------------------------------------


@dataclass
class Resolved:
    team_id: str | None = None
    project_id: str | None = None
    milestone_id: str | None = None
    label_ids: tuple[str, ...] | None = None


def resolve(tracker: Tracker, draft: Draft) -> tuple[Resolved, list[str]]:
    """The draft's names as tracker ids, and a refusal line per name that does not resolve."""
    ids = Resolved()
    problems: list[str] = []
    if draft.get("team"):
        ids.team_id = tracker.resolve_team(draft.get("team"))
        if ids.team_id is None:
            problems.append(f"I8 team: `{draft.get('team')}` does not resolve to a team.")
    if draft.get("project"):
        ids.project_id = tracker.resolve_project(draft.get("project"))
        if ids.project_id is None:
            problems.append(f"I5 project: `{draft.get('project')}` does not resolve to a project.")
    if ids.project_id and draft.get("milestone"):
        ids.milestone_id = tracker.resolve_milestone(ids.project_id, draft.get("milestone"))
        if ids.milestone_id is None:
            problems.append(
                f"I6 milestone: `{draft.get('milestone')}` is not a milestone of the project."
            )
    problems += _resolve_labels(tracker, draft, ids)
    return ids, problems


def _resolve_labels(tracker: Tracker, draft: Draft, ids: Resolved) -> list[str]:
    names = draft.labels()
    if not names:
        return []
    found = tracker.resolve_labels(names)
    ids.label_ids = tuple(found[n] for n in names if found.get(n))
    return [f"I8 label: `{n}` does not resolve to a label." for n in names if not found.get(n)]


def issue_input(draft: Draft, ids: Resolved) -> IssueInput:
    return IssueInput(
        title=draft.get("title"),
        body=draft.body,
        priority=int(draft.get("priority")),
        team_id=ids.team_id,
        project_id=ids.project_id,
        milestone_id=ids.milestone_id,
        label_ids=ids.label_ids,
    )


# --- the audit and its ratchet -------------------------------------------------


def run_audit(tracker: Tracker, now: datetime | None = None) -> dict[str, int]:
    """The P1 to P4 counts over the tracker's active issues and projects."""
    counts = dict.fromkeys(RULE_KEYS, 0)
    for issue in tracker.active_issues():
        counts["P1"] += not issue.has_milestone
        counts["P2"] += not issue.has_project
        counts["P4"] += count_words(issue.body) > WORD_LIMIT
    cutoff = (now or datetime.now(UTC)) - timedelta(days=PROJECT_UPDATE_WINDOW_DAYS)
    for project in tracker.active_projects():
        counts["P3"] += project.last_update is None or project.last_update < cutoff
    return counts


def load_baseline(path: Path, scope: str) -> dict[str, int] | None:
    """The stored counts, or ``None`` when no baseline was seeded yet."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DraftError(f"baseline {path} is unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != BASELINE_SCHEMA:
        raise DraftError("baseline schema does not match; reseed with --update-baseline.")
    if data.get("scope") != scope:
        raise DraftError(f"baseline is for {data.get('scope')!r}, not {scope!r}; reseed it.")
    counts = data.get("counts")
    if not isinstance(counts, dict) or any(not isinstance(counts.get(k), int) for k in RULE_KEYS):
        raise DraftError("baseline is missing a rule count; reseed with --update-baseline.")
    return {k: counts[k] for k in RULE_KEYS}


def write_baseline(path: Path, scope: str, counts: dict[str, int], today: str) -> None:
    body = {
        "schema": BASELINE_SCHEMA,
        "scope": scope,
        "counts": {k: counts[k] for k in RULE_KEYS},
        "seeded_at": today,
    }
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")


def ratchet(counts: dict[str, int], baseline: dict[str, int] | None) -> list[str]:
    """The rules whose count rose above the baseline. No baseline never fails."""
    if baseline is None:
        return []
    return [k for k in RULE_KEYS if counts[k] > baseline[k]]
