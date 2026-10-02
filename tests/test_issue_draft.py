"""The issue writer: the draft rules, the audit ratchet and loop-issue's exit codes.

No test reaches a tracker. The command is given a fake ``Tracker`` in place of
the adapter, and the Linear client has its own tests over a fake transport.
Every dash is built with ``chr()``, so this file passes the rule it tests.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core import issue_draft as idr
from core import issue_writer as iw
from core.config import DEFAULTS, LoopConfig

EM_DASH = chr(0x2014)
EN_DASH = chr(0x2013)
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)

GOOD_BODY = "\n".join(
    [
        "## Effect",
        "The base ref is stale, so a run that never fetched sees the wrong diff.",
        "## Now",
        "The hook diffs the wrong ref and passes work it should have caught.",
        "## Picture",
        "```mermaid",
        "flowchart LR",
        "  A[run] --> B{fetch}",
        "```",
        "## Do",
        "Fetch before the diff. Compare against the fetched ref.",
        "## Done when",
        "The hook diffs the fetched ref and a stale pointer hides nothing.",
    ]
)


def _text(body: str = GOOD_BODY, **over: str) -> str:
    fields = {
        "title": "Pre-push ratchet diffs against the stale remote pointer",
        "team": "Platform",
        "project": "Delivery",
        "milestone": "Hardening",
        "priority": "2",
    }
    fields.update(over)
    lines = ["---", *(f"{k}: {v}" for k, v in fields.items() if v is not None), "---", "", body]
    return "\n".join(lines)


def _draft(body: str = GOOD_BODY, config: LoopConfig = DEFAULTS, **over: str) -> idr.Draft:
    return idr.parse_draft(_text(body, **over), config)


def _body(effect: str = "a", now: str = "b", do: str = "c", done: str = "d") -> str:
    return f"## Effect\n{effect}\n## Now\n{now}\n## Do\n{do}\n## Done when\n{done}"


class FakeTracker:
    """A ``Tracker`` over fixed names; it records every write."""

    name = "linear"

    def __init__(self, issues=(), projects=(), missing: tuple[str, ...] = ()) -> None:
        self.issues, self.projects, self.missing = list(issues), list(projects), missing
        self.writes: list[tuple[str, object, idr.IssueInput]] = []

    def _id(self, kind: str, name: str) -> str | None:
        return None if name in self.missing else f"{kind}:{name}"

    def resolve_team(self, name):
        return self._id("team", name)

    def resolve_project(self, name):
        return self._id("project", name)

    def resolve_milestone(self, project_id, name):
        return self._id("milestone", name)

    def resolve_labels(self, names):
        return {n: self._id("label", n) for n in names}

    def create_issue(self, issue):
        self.writes.append(("create", None, issue))
        return idr.Written("ENG-9", "https://tracker.example/ENG-9")

    def update_issue(self, reference, issue):
        self.writes.append(("update", reference, issue))
        return idr.Written(reference, f"https://tracker.example/{reference}")

    def active_issues(self):
        return iter(self.issues)

    def active_projects(self):
        return iter(self.projects)


# --- front matter ----------------------------------------------------------------


def test_front_matter_splits_from_the_body() -> None:
    draft = _draft()
    assert draft.get("title").startswith("Pre-push")
    assert draft.body.startswith("## Effect")


def test_a_value_with_a_colon_keeps_the_rest() -> None:
    fields, _ = idr.parse_front_matter("---\ntitle: a: b: c\n---\nbody")
    assert fields["title"] == "a: b: c"


@pytest.mark.parametrize(
    "text",
    [
        "no front matter here",
        "---\ntitle: a\ntitle: b\n---\nx",
        "---\ntitle: a\nbody with no close",
        "---\nnot a pair\n---\nx",
        "---\nprojct: typo\n---\nx",
    ],
    ids=["missing", "duplicate", "unclosed", "no-colon", "unknown-key"],
)
def test_a_malformed_front_matter_is_a_usage_error(text: str) -> None:
    with pytest.raises(idr.DraftError):
        idr.parse_front_matter(text)


def test_a_draft_inherits_the_tracker_names_loop_toml_sets() -> None:
    # Value: protects=a host names its team once, in loop.toml; fails_when=the [tracker]
    # names are ignored and every draft is refused for a missing team; why_new=issue writer
    config = LoopConfig(tracker_team="Platform", tracker_project="Delivery", tracker_milestone="M1")
    draft = _draft(config=config, team="", project=None, milestone="Own")
    assert (draft.get("team"), draft.get("project"), draft.get("milestone")) == (
        "Platform",
        "Delivery",
        "Own",
    )
    assert idr.lint_offline(draft, config) == []


# --- blocks and words ------------------------------------------------------------


def test_a_picture_block_with_only_a_fence_is_not_empty() -> None:
    assert idr.lint_blocks(GOOD_BODY) is None


def test_a_heading_inside_a_fence_is_not_a_heading() -> None:
    body = "## Effect\nx\n```\n## Now\n```\n## Now\ny\n## Do\nz\n## Done when\nw"
    assert idr.lint_blocks(body) is None


def test_the_word_count_skips_fences_and_headings() -> None:
    body = "## Effect\none two three\n```\na b c d e f g h i j\n```\n" + _body("", "x", "y", "z")
    assert idr.count_words(body) == 3 + 1 + 1 + 1


# --- the offline rules -------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "expected"),
    [("ends with a period.", "period"), ("x" * 81, "80"), ("", "add a `title:`")],
)
def test_i1_refuses_a_bad_title(title: str, expected: str) -> None:
    problem = idr.lint_title(_draft(title=title))
    assert problem.startswith("I1") and expected in problem


def test_i1_passes_a_clean_title() -> None:
    assert idr.lint_title(_draft(title="a clean title")) is None


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("## Effect\na\n## Now\nb\n## Do\nc", "`Done when` block is missing"),
        ("## Effect\na\n## Now\nb\n## Wat\nc\n## Do\nd\n## Done when\ne", "unexpected"),
        ("## Now\nb\n## Effect\na\n## Do\nc\n## Done when\nd", "order"),
        (_body() + "\n## Do\nagain", "duplicate"),
        (_body(now="   "), "`Now` block is empty"),
    ],
    ids=["missing", "unknown", "order", "duplicate", "empty"],
)
def test_i2_refuses_a_bad_block_layout(body: str, expected: str) -> None:
    problem = idr.lint_blocks(body)
    assert problem.startswith("I2") and expected in problem


def test_i3_refuses_a_body_over_the_word_limit() -> None:
    assert idr.lint_words(_body(effect="word " * 200)).startswith("I3")
    assert idr.lint_words(GOOD_BODY) is None


@pytest.mark.parametrize("dash", [EM_DASH, EN_DASH, chr(0x2012), chr(0x2015)])
def test_i4_refuses_every_dash_the_dash_rule_bans(dash: str) -> None:
    assert idr.lint_dashes(_draft(title=f"a {dash} b")).startswith("I4 dash: the title")
    assert idr.lint_dashes(_draft(_body(do=f"an en{dash}dash"))).startswith("I4 dash: the body")


def test_i4_allows_the_hyphen() -> None:
    assert idr.lint_dashes(_draft(_body(do="another-one"), title="a hyphen-word")) is None


@pytest.mark.parametrize(
    ("priority", "ok"), [("9", False), ("high", False), ("", False), ("2", True)]
)
def test_i7_takes_a_priority_of_one_to_four(priority: str, ok: bool) -> None:
    problem = idr.lint_priority(_draft(priority=priority))
    assert (problem is None) is ok
    assert ok or problem.startswith("I7")


def test_i5_i6_i8_refuse_a_missing_name() -> None:
    problems = idr.lint_present(_draft(project="", milestone="", team=""))
    assert [p.split(":")[0] for p in problems] == ["I5 project", "I6 milestone", "I8 team"]


def test_i8_refuses_a_malformed_reference_and_passes_good_ones() -> None:
    assert "ENG-12x" in idr.lint_references("see ENG-12x for detail")
    assert idr.lint_references("see ENG-126 and abc-10.") is None
    # With the default prefix a hyphenated word is not a reference.
    assert idr.lint_references("the pre-push hook and a well-known case") is None


def test_i8_reads_the_reference_pattern_from_the_config() -> None:
    # Value: protects=a host's own prefix decides what a reference is; fails_when=a
    # hardcoded prefix judges another tracker's ids; why_new=the original read one team's prefix
    config = LoopConfig(tracker_prefix="ENG")
    assert "ENG-abc" in idr.lint_references("see ENG-abc", config)
    assert idr.lint_references("see ENG-12 and OPS-x", config) is None
    strict = LoopConfig(tracker_prefix="ENG", tracker_pattern=r"(ENG-\d{3})")
    assert "ENG-12" in idr.lint_references("see ENG-12", strict)
    assert idr.lint_references("see ENG-123", strict) is None


def test_a_clean_draft_has_no_offline_problem() -> None:
    assert idr.lint_offline(_draft()) == []


# --- resolution through a tracker --------------------------------------------------


def test_resolve_returns_the_ids_of_every_name() -> None:
    ids, problems = idr.resolve(FakeTracker(), _draft(labels="tooling, infra"))
    assert problems == []
    assert ids.team_id == "team:Platform"
    assert ids.project_id == "project:Delivery"
    assert ids.milestone_id == "milestone:Hardening"
    assert ids.label_ids == ("label:tooling", "label:infra")


def test_resolve_names_every_name_that_does_not_resolve() -> None:
    tracker = FakeTracker(missing=("Platform", "Hardening", "infra"))
    _, problems = idr.resolve(tracker, _draft(labels="tooling, infra"))
    assert [p.split(":")[0] for p in problems] == ["I8 team", "I6 milestone", "I8 label"]


def test_an_issue_input_leaves_out_what_the_draft_does_not_name() -> None:
    issue = idr.issue_input(_draft(), idr.Resolved(team_id="T"))
    assert issue.priority == 2
    assert issue.label_ids is None and issue.milestone_id is None


# --- the audit and the ratchet -----------------------------------------------------


def _counts(**over: int) -> dict[str, int]:
    return {"P1": 0, "P2": 0, "P3": 0, "P4": 0, **over}


def test_the_audit_counts_each_rule() -> None:
    tracker = FakeTracker(
        issues=[
            idr.AuditIssue(has_milestone=False, has_project=False, body="x"),
            idr.AuditIssue(has_milestone=True, has_project=True, body="word " * 200),
        ],
        projects=[
            idr.AuditProject("stale", NOW - timedelta(days=31)),
            idr.AuditProject("fresh", NOW - timedelta(days=2)),
            idr.AuditProject("never", None),
        ],
    )
    assert idr.run_audit(tracker, NOW) == _counts(P1=1, P2=1, P3=2, P4=1)


def test_a_baseline_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "b.json"
    idr.write_baseline(path, "linear", _counts(P1=5, P2=57), "2026-09-21")
    assert idr.load_baseline(path, "linear") == _counts(P1=5, P2=57)
    assert json.loads(path.read_text(encoding="utf-8"))["seeded_at"] == "2026-09-21"


def test_an_absent_baseline_is_none(tmp_path: Path) -> None:
    assert idr.load_baseline(tmp_path / "missing.json", "linear") is None


@pytest.mark.parametrize(
    "data",
    [
        {"schema": 1, "scope": "other", "counts": _counts()},
        {"schema": 2, "scope": "linear", "counts": _counts()},
        {"schema": 1, "scope": "linear", "counts": {"P1": 0}},
        "not json",
    ],
    ids=["scope", "schema", "count", "malformed"],
)
def test_a_baseline_that_does_not_fit_is_a_usage_error(tmp_path: Path, data: object) -> None:
    path = tmp_path / "b.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    with pytest.raises(idr.DraftError):
        idr.load_baseline(path, "linear")


def test_the_ratchet_flags_only_a_risen_rule() -> None:
    assert idr.ratchet(_counts(P2=58), _counts(P2=57)) == ["P2"]
    assert idr.ratchet(_counts(P2=56), _counts(P2=57)) == []
    assert idr.ratchet(_counts(P2=99), None) == []


# --- loop-issue ------------------------------------------------------------------


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeTracker:
    tracker = FakeTracker()
    monkeypatch.setattr(iw, "connect", lambda name, required: tracker)
    return tracker


def _write(tmp_path: Path, text: str) -> str:
    path = tmp_path / "draft.md"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_clean_draft_without_a_key_lints_ok(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    assert iw.main([_write(tmp_path, _text())]) == 0
    assert "names not resolved" in capsys.readouterr().out


def test_a_bad_draft_exits_one_with_a_line_per_rule(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    draft = _write(tmp_path, _text(title="ends with a period.", priority="9"))
    assert iw.main([draft]) == 1
    out = capsys.readouterr().out.splitlines()
    assert [line[:2] for line in out] == ["I1", "I7"]


def test_a_missing_draft_exits_two(tmp_path) -> None:
    assert iw.main([str(tmp_path / "nope.md")]) == 2


def test_a_write_without_the_key_exits_two(tmp_path, monkeypatch, capsys) -> None:
    # Value: protects=CI and a person see a missing key as an environment fault, not a
    # refusal; fails_when=the write runs or exits 1; why_new=the shared 0/1/2 contract
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    assert iw.main([_write(tmp_path, _text()), "--apply"]) == 2
    assert "LINEAR_API_KEY" in capsys.readouterr().err
    assert iw.main(["--audit", "--baseline", str(tmp_path / "b.json")]) == 2


def test_apply_writes_a_clean_draft_once(tmp_path, fake, capsys) -> None:
    assert iw.main([_write(tmp_path, _text()), "--apply"]) == 0
    assert [w[0] for w in fake.writes] == ["create"]
    assert "wrote ENG-9" in capsys.readouterr().out


def test_apply_refuses_an_unresolved_name_and_writes_nothing(tmp_path, fake) -> None:
    fake.missing = ("Delivery",)
    assert iw.main([_write(tmp_path, _text()), "--apply"]) == 1
    assert fake.writes == []


def test_update_targets_the_issue_the_draft_names(tmp_path, fake) -> None:
    assert iw.main([_write(tmp_path, _text(issue="ENG-4")), "--update"]) == 0
    assert fake.writes[0][:2] == ("update", "ENG-4")
    assert iw.main([_write(tmp_path, _text()), "--update"]) == 2


@pytest.mark.parametrize(
    "argv",
    [[], ["d.md", "--apply", "--update"], ["d.md", "--audit"], ["--update-baseline"]],
)
def test_a_wrong_combination_of_flags_exits_two(argv: list[str], fake) -> None:
    assert iw.main(argv) == 2


def test_apply_is_refused_during_a_run(start_run, monkeypatch, tmp_path, fake) -> None:
    # Value: protects=a run writes only to its worktree and its pull request;
    # fails_when=an agent files an issue through loop-issue mid-run; why_new=ported refusal
    _, worktree = start_run()
    draft = _write(tmp_path, _text())
    monkeypatch.chdir(worktree)
    assert iw.main([draft, "--apply"]) == 1
    assert fake.writes == []
    assert iw.main([draft]) == 0


def test_the_audit_seeds_then_holds_the_ratchet(tmp_path, fake, capsys) -> None:
    baseline = str(tmp_path / "b.json")
    fake.issues = [idr.AuditIssue(has_milestone=False, has_project=True, body="x")]
    assert iw.main(["--audit", "--baseline", baseline]) == 0
    assert "no baseline yet" in capsys.readouterr().out
    assert iw.main(["--audit", "--update-baseline", "--baseline", baseline]) == 0
    assert iw.main(["--audit", "--baseline", baseline]) == 0
    fake.issues.append(idr.AuditIssue(has_milestone=False, has_project=True, body="y"))
    assert iw.main(["--audit", "--baseline", baseline]) == 1
    assert iw.main(["--audit", "--update-baseline", "--baseline", baseline]) == 1
    assert json.loads(Path(baseline).read_text(encoding="utf-8"))["counts"]["P1"] == 1


def test_the_audit_baseline_defaults_to_the_repository_root(make_repo, tmp_path, monkeypatch, fake):
    repo = make_repo(tmp_path / "host")
    (repo / "sub").mkdir()
    monkeypatch.chdir(repo / "sub")
    assert iw.main(["--audit", "--update-baseline"]) == 0
    assert (repo / iw.BASELINE_FILE).is_file()


def test_a_failed_read_is_never_scored(tmp_path, fake) -> None:
    def broken():
        raise idr.TrackerError("incomplete page")

    fake.active_issues = broken
    baseline = tmp_path / "b.json"
    assert iw.main(["--audit", "--update-baseline", "--baseline", str(baseline)]) == 2
    assert not baseline.exists()
