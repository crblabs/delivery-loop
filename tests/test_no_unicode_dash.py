"""The no-unicode-dash rule.

Every dash the test needs is built with `chr()`, so this file carries no literal
dash and does not trip the rule it checks. The tool's own module has the same
property, so the gate does not flag the gate.
"""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from core import no_unicode_dash as nd

FIGURE_DASH = chr(0x2012)
EN_DASH = chr(0x2013)
EM_DASH = chr(0x2014)
HORIZONTAL_BAR = chr(0x2015)
MINUS_SIGN = chr(0x2212)  # out of range
HYPHEN = "-"


# ------------------------------------------------------------- matcher


@pytest.mark.parametrize("ch", [FIGURE_DASH, EN_DASH, EM_DASH, HORIZONTAL_BAR])
def test_every_banned_codepoint_is_flagged(ch: str) -> None:
    found = nd.dashes_in_text(f"a{ch}b")
    assert found == [(2, ord(ch))]


@pytest.mark.parametrize("ch", [MINUS_SIGN, HYPHEN, "x"])
def test_out_of_range_characters_are_not_flagged(ch: str) -> None:
    assert nd.dashes_in_text(f"a{ch}b") == []


def test_column_is_one_based() -> None:
    assert nd.dashes_in_text(f"{EM_DASH}") == [(1, 0x2014)]


# ------------------------------------------------------------- inline pragma


def test_inline_pragma_with_reason_excepts() -> None:
    line = f"pattern = r'[{EN_DASH}]'  # {nd.RULE_ID}: a regex character range"
    assert nd.has_inline_pragma(line) is True


def test_bare_pragma_does_not_except() -> None:
    line = f"x = '{EN_DASH}'  # {nd.RULE_ID}:   "
    assert nd.has_inline_pragma(line) is False


def test_check_text_honors_the_pragma_per_line() -> None:
    text = f"clean line\nbad {EM_DASH} line\nok {EN_DASH} here  # {nd.RULE_ID}: quoted source"
    violations = nd.check_text(text, "<t>")
    assert len(violations) == 1
    assert violations[0].line == 2


# ------------------------------------------------------------- policy


def test_policy_accepts_an_exact_path_line_reason() -> None:
    policy = nd.parse_policy(
        '{"exceptions": [{"path": "tools/x.py", "line": 5, "reason": "functional literal"}]}',
        "<t>",
    )
    assert policy.excepts("tools/x.py", 5) is True
    assert policy.excepts("tools/x.py", 6) is False


@pytest.mark.parametrize(
    "entry",
    [
        '{"path": "tools/*.py", "line": 1, "reason": "glob"}',
        '{"path": "../x.py", "line": 1, "reason": "traversal"}',
        '{"path": "/abs/path", "line": 1, "reason": "absolute"}',
        '{"path": "tools/x.py", "line": 0, "reason": "zero line"}',
        '{"path": "tools/x.py", "line": 1, "reason": "   "}',
        '{"path": "tools/x.py", "reason": "no line"}',
        '{"path": "tools/x.py", "line": 1}',
    ],
)
def test_policy_rejects_bad_entries(entry: str) -> None:
    with pytest.raises(nd.DashError):
        nd.parse_policy(f'{{"exceptions": [{entry}]}}', "<t>")


def test_policy_accepts_dotdot_inside_a_filename() -> None:
    # ".." must be rejected as a path component, not as a substring.
    policy = nd.parse_policy(
        '{"exceptions": [{"path": "docs/version..md", "line": 1, "reason": "ok"}]}', "<t>"
    )
    assert policy.excepts("docs/version..md", 1) is True


def test_policy_rejects_non_object_and_non_list() -> None:
    with pytest.raises(nd.DashError):
        nd.parse_policy("[]", "<t>")
    with pytest.raises(nd.DashError):
        nd.parse_policy('{"exceptions": {}}', "<t>")
    with pytest.raises(nd.DashError):
        nd.parse_policy('{"exclude": "data/"}', "<t>")


# ------------------------------------------------------------- excluded dirs


def test_nothing_is_excluded_without_a_policy() -> None:
    assert nd.EMPTY_POLICY.is_excluded("data/x.csv") is False


@pytest.mark.parametrize(
    "path,excluded",
    [
        ("data/x.csv", True),
        ("tests/fixtures/x.json", True),
        ("tools/x.py", False),
        ("tests/test_x.py", False),
        ("database/x.md", False),
    ],
)
def test_policy_excludes_by_directory_prefix(path: str, excluded: bool) -> None:
    policy = nd.parse_policy('{"exclude": ["data/", "tests/fixtures/"]}', "<t>")
    assert policy.is_excluded(path) is excluded


@pytest.mark.parametrize("prefix", ["data", "data/*/", "/data/", "../data/", "", 3])
def test_policy_rejects_a_bad_exclude(prefix: object) -> None:
    with pytest.raises(nd.DashError):
        nd.parse_policy(json.dumps({"exclude": [prefix]}), "<t>")


# ------------------------------------------------------------- patch parser


def test_added_line_is_parsed_with_its_new_number() -> None:
    diff = "@@ -0,0 +5,1 @@\n+hello\n"
    assert nd._parse_added(diff) == [(5, "hello")]


def test_deletion_only_produces_no_added_line() -> None:
    diff = f"@@ -5,1 +4,0 @@\n-old {EM_DASH} line\n"
    assert nd._parse_added(diff) == []


def test_moved_line_shows_its_added_side() -> None:
    # A move is a deletion plus an addition; the addition is an added line, so a
    # moved dashed line must comply.
    diff = f"@@ -3,1 +3,0 @@\n-x {EN_DASH} y\n@@ -9,0 +10,1 @@\n+x {EN_DASH} y\n"
    added = nd._parse_added(diff)
    assert added == [(10, f"x {EN_DASH} y")]
    assert nd.dashes_in_text(added[0][1]) != []


def test_added_line_beginning_with_plus_is_not_a_header() -> None:
    # A body line whose content starts with '+' must not be read as the '+++'
    # file header.
    diff = "@@ -0,0 +1,1 @@\n++two pluses\n"
    assert nd._parse_added(diff) == [(1, "+two pluses")]


def test_lines_before_the_first_hunk_are_ignored() -> None:
    diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -0,0 +1,1 @@\n+body\n"
    assert nd._parse_added(diff) == [(1, "body")]


def test_no_newline_marker_is_not_a_line() -> None:
    diff = "@@ -0,0 +1,1 @@\n+last\n\\ No newline at end of file\n"
    assert nd._parse_added(diff) == [(1, "last")]


def test_parse_added_keeps_an_embedded_cr() -> None:
    # A carriage return inside an added line must not split it (that would drop
    # the leading + and hide a dash after the CR). The parser splits on LF only.
    diff = f"@@ -0,0 +1,1 @@\n+a\rb{EM_DASH}\n"
    added = nd._parse_added(diff)
    assert added == [(1, f"a\rb{EM_DASH}")]
    assert nd.dashes_in_text(added[0][1]) != []


# ------------------------------------------------------------- numstat parser


def test_numstat_drops_binary_keeps_text() -> None:
    z = "\0".join(["3\t1\ttools/a.py", "-\t-\tassets/logo.png", "0\t0\tdata/b.csv"]) + "\0"
    assert nd._parse_numstat(z) == ["tools/a.py", "data/b.csv"]


def test_numstat_keeps_a_tab_inside_a_filename() -> None:
    # split at most twice, so a tab in the path is not truncated (which would
    # scan nothing and let a dash escape).
    z = "1\t0\tdocs/a\tb.md" + "\0"
    assert nd._parse_numstat(z) == ["docs/a\tb.md"]


def test_numstat_reads_the_new_path_of_a_rename() -> None:
    # A rename record has an empty inline path, then old and new as NUL fields.
    z = "\0".join(["2\t0\t", "tools/old.py", "tools/new.py"]) + "\0"
    assert nd._parse_numstat(z) == ["tools/new.py"]


# ------------------------------------------------------------- message


def test_message_names_location_codepoint_rule_and_exception() -> None:
    msg = nd.Violation("docs/x.md", 12, 8, 0x2014).message()
    assert "docs/x.md:12:8" in msg
    assert "U+2014" in msg
    assert "EM DASH" in msg
    assert nd.RULE_ID in msg
    assert "hyphen" in msg
    assert nd.EXCEPTIONS_PATH in msg


# ------------------------------------------------------------- base resolution


def test_base_revision_fails_loud_when_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess([], 128, "", "fatal: no merge base")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(nd.DashError):
        nd.base_revision(tmp_path, "origin/main", None)


# ------------------------------------------------------------- gate and hook agree


@pytest.mark.parametrize("ch", [FIGURE_DASH, EN_DASH, EM_DASH, HORIZONTAL_BAR])
def test_gate_matcher_and_hook_matcher_agree_on_every_byte(ch: str) -> None:
    # The gate scans a line with dashes_in_text; a stdin or editor check scans
    # new content with check_text. Both use the same matcher, so a byte one
    # flags the other flags too.
    line = f"a {ch} b"
    gate = nd.dashes_in_text(line)
    hook = nd.check_text(line, "<edit>")
    assert bool(gate) is bool(hook)


# ------------------------------------------------------------- a real repository


@pytest.fixture
def repo(
    make_repo: Callable[[Path], Path],
    git: Callable[..., str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """A host repository whose main already holds a dash, on a feature branch."""
    root = make_repo(tmp_path / "host")
    (root / "old.md").write_text(f"legacy {EM_DASH} line\n", encoding="utf-8")
    git(root, "add", "old.md")
    git(root, "commit", "-q", "-m", "legacy")
    git(root, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(root)
    return root


def _commit(git: Callable[..., str], root: Path, name: str, text: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git(root, "add", name)
    git(root, "commit", "-q", "-m", name)


def test_a_preexisting_dash_is_not_flagged(repo: Path) -> None:
    # Value: protects=the ratchet only gates added lines; fails_when=the gate
    # reads whole files; why_new=the rule moved into the package; seam=git diff
    assert nd.scan_range(repo, "main", None) == []


def test_an_added_dash_is_flagged_with_its_line(repo: Path, git: Callable[..., str]) -> None:
    _commit(git, repo, "new.md", f"fine\nbad {EN_DASH} here\n")
    found = nd.scan_range(repo, "main", None)
    assert [(v.path, v.line, v.codepoint) for v in found] == [("new.md", 2, 0x2013)]


def test_an_untracked_file_counts_in_worktree_mode(repo: Path) -> None:
    (repo / "draft.md").write_text(f"x {EM_DASH} y\n", encoding="utf-8")
    assert [v.path for v in nd.scan_range(repo, "main", None)] == ["draft.md"]


def test_head_mode_reads_the_policy_at_head(repo: Path, git: Callable[..., str]) -> None:
    _commit(git, repo, "data/raw.csv", f"a,{EM_DASH}\n")
    _commit(git, repo, "notes.md", f"one\ntwo {EM_DASH}\n")
    head = git(repo, "rev-parse", "HEAD").strip()
    assert {v.path for v in nd.scan_range(repo, "main", head)} == {"data/raw.csv", "notes.md"}
    policy = {"exclude": ["data/"], "exceptions": [{"path": "notes.md", "line": 2, "reason": "r"}]}
    _commit(git, repo, nd.EXCEPTIONS_PATH, json.dumps(policy))
    head = git(repo, "rev-parse", "HEAD").strip()
    assert nd.scan_range(repo, "main", head) == []


def test_a_broken_policy_is_a_fault(repo: Path) -> None:
    (repo / nd.EXCEPTIONS_PATH).write_text("{not json", encoding="utf-8")
    with pytest.raises(nd.DashError):
        nd.scan_range(repo, "main", None)


def test_audit_reads_every_tracked_line_under_a_path(repo: Path, git: Callable[..., str]) -> None:
    _commit(git, repo, "docs/a.md", f"{FIGURE_DASH}\n")
    assert [v.path for v in nd.audit(repo, ["docs"])] == ["docs/a.md"]
    assert {v.path for v in nd.audit(repo, [])} == {"docs/a.md", "old.md"}


# ------------------------------------------------------------- the command


def test_main_exit_codes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert nd.main(["--base", "main"]) == 0
    (repo / "draft.md").write_text(f"x {EM_DASH} y\n", encoding="utf-8")
    assert nd.main(["--base", "main"]) == 1
    assert "draft.md:1:3" in capsys.readouterr().err
    assert nd.main(["--base", "no-such-ref"]) == 2


def test_main_reads_stdin(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(f"subject\n\nbody {HORIZONTAL_BAR}\n"))
    assert nd.main(["--stdin-text", "--label", "commit message"]) == 1
    assert "commit message:3:6" in capsys.readouterr().err


def test_main_outside_a_repository_is_a_fault() -> None:
    # The autouse fixture puts the test in an empty directory, not a repository.
    assert nd.main([]) == 2


# ------------------------------------------------------------- the stages run it

PLUGIN = Path(__file__).resolve().parent.parent


def test_the_launcher_runs_the_rule_in_a_host_repository(repo: Path) -> None:
    # Value: protects=a stage calls the rule by name from any host repo;
    # fails_when=bin/loop-no-dash cannot find core; why_new=the stages run it; seam=PATH
    launcher = PLUGIN / "bin" / "loop-no-dash"
    clean = subprocess.run([launcher, "--base", "main"], cwd=repo, capture_output=True, text=True)
    assert clean.returncode == 0, clean.stderr
    (repo / "draft.md").write_text(f"x {EM_DASH} y\n", encoding="utf-8")
    dirty = subprocess.run([launcher, "--base", "main"], cwd=repo, capture_output=True, text=True)
    assert dirty.returncode == 1
    assert "draft.md:1:3" in dirty.stderr


@pytest.mark.parametrize("stage", ["implement", "qa", "review", "ship"])
def test_every_stage_that_commits_or_ships_runs_the_rule(stage: str) -> None:
    # Value: protects=a run cannot land a dash unchecked; fails_when=a stage prompt
    # drops the check; why_new=the loop stopped running it after the move; seam=prompt
    text = (PLUGIN / "skills" / "pipeline" / "stages" / f"{stage}.md").read_text("utf-8")
    assert "loop-no-dash --base" in text
    assert "loop-no-dash --stdin-text" in text
