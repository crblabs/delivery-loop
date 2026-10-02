"""The comment-hygiene rule.

Each test writes Python into a host repository on a feature branch and gates the
diff against main. The ERA001 tests need ruff on PATH; the rest do not, so they
run with that check off and pass on a host without ruff.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from core import comments_gate as cg

PLUGIN = Path(__file__).resolve().parent.parent
needs_ruff = pytest.mark.skipif(shutil.which("ruff") is None, reason="ruff is not on PATH")

BLOCK3 = "def f():\n    # one\n    # two\n    # three\n    return 1\n"
BLOCK4 = "def f():\n    # one\n    # two\n    # three\n    # four\n    return 1\n"
PRAGMA_OK = (
    "def f():\n    # comments: dense on purpose\n"
    "    # one\n    # two\n    # three\n    # four\n    return 1\n"
)
PRAGMA_BLANK = (
    "def f():\n    # comments:\n    # one\n    # two\n    # three\n    # four\n    return 1\n"
)
DOC_PUB = 'def h():\n    """One.\n    Two.\n    Three.\n    Four.\n    """\n    return 1\n'
DOC_PRIV = 'def _h():\n    """One.\n    Two.\n    Three.\n    Four.\n    """\n    return 1\n'


@pytest.fixture
def repo(
    make_repo: Callable[[Path], Path],
    git: Callable[..., str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """A host repository whose main holds one Python file, on a feature branch."""
    root = make_repo(tmp_path / "host")
    _write(root, "tools/base.py", "V = 1\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(root)
    return root


def _write(root: Path, relpath: str, text: str) -> None:
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit_on_main(root: Path, git: Callable[..., str], relpath: str, text: str) -> None:
    """Land `text` on main and rebase the feature branch onto it, so it is base."""
    git(root, "checkout", "-q", "main")
    _write(root, relpath, text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", relpath)
    git(root, "checkout", "-q", "feature")
    git(root, "reset", "-q", "--hard", "main")


def _scan(root: Path, era001: bool = False) -> list[cg.Violation]:
    return cg.scan_range(root, "main", None, era001=era001)


def _details(root: Path, era001: bool = False) -> list[str]:
    return [v.detail for v in _scan(root, era001)]


# ------------------------------------------------------------- blocks


def test_a_three_line_block_passes(repo: Path) -> None:
    _write(repo, "tools/y.py", BLOCK3)
    assert _scan(repo) == []


def test_a_block_over_three_lines_is_refused(repo: Path) -> None:
    _write(repo, "tools/y.py", BLOCK4)
    assert _details(repo) == ["comment block of 4 lines (max 3)"]


def test_appending_to_a_three_line_block_is_refused(repo: Path, git: Callable[..., str]) -> None:
    # The whole resulting block is measured, not just the added line.
    _commit_on_main(repo, git, "tools/x.py", BLOCK3)
    _write(repo, "tools/x.py", BLOCK4)
    assert len(_scan(repo)) == 1


def test_an_untouched_long_block_passes(repo: Path, git: Callable[..., str]) -> None:
    # Added lines only: a long block already in the base does not trip.
    five = "def f():\n    # a\n    # b\n    # c\n    # d\n    # e\n    return 1\n"
    _commit_on_main(repo, git, "tools/x.py", five)
    _write(repo, "tools/x.py", five + "\n\ndef g():\n    return 2\n")
    assert _scan(repo) == []


def test_a_hash_inside_a_string_is_not_a_comment(repo: Path) -> None:
    _write(repo, "tools/y.py", 'def f():\n    s = "see ABC-9 / #49"\n    return s\n')
    assert _scan(repo) == []


def test_the_pragma_excuses_a_dense_block(repo: Path) -> None:
    _write(repo, "tools/y.py", PRAGMA_OK)
    assert _scan(repo) == []


def test_a_pragma_with_a_blank_reason_does_not_excuse(repo: Path) -> None:
    _write(repo, "tools/y.py", PRAGMA_BLANK)
    assert len(_scan(repo)) == 1


# ------------------------------------------------------------- docstrings


def test_a_module_docstring_is_exempt(repo: Path) -> None:
    _write(repo, "tools/y.py", '"""One.\nTwo.\nThree.\nFour.\nFive.\n"""\n\nV = 1\n')
    assert _scan(repo) == []


def test_a_public_function_docstring_is_exempt(repo: Path) -> None:
    _write(repo, "tools/y.py", DOC_PUB)
    assert _scan(repo) == []


def test_a_long_private_docstring_is_refused(repo: Path) -> None:
    _write(repo, "tools/y.py", DOC_PRIV)
    assert _details(repo) == ["docstring of 5 lines (max 3)"]


def test_the_pragma_excuses_a_private_docstring(repo: Path) -> None:
    body = (
        "def _h():\n    # comments: the maths needs the derivation\n"
        '    """One.\n    Two.\n    Three.\n    Four.\n    """\n    return 1\n'
    )
    _write(repo, "tools/y.py", body)
    assert _scan(repo) == []


# ------------------------------------------------------------- ids


@pytest.mark.parametrize(
    "comment",
    [
        "see ABC-9",
        "closes #49",
        "fixes #9",
        "https://github.com/o/r/pull/12",
        "https://gitlab.example/o/r/-/merge_requests/3",
    ],
)
def test_an_id_in_a_comment_is_refused(
    repo: Path, comment: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _write(repo, "tools/y.py", f"def f():\n    x = 1  # {comment}\n    return x\n")
    assert cg.main(["--base", "main"]) == 1
    assert "ticket or PR id" in capsys.readouterr().err


@pytest.mark.parametrize("comment", ["UTF-8 on the wire", "SHA-256 of the body", "a #define"])
def test_a_standard_name_is_not_an_id(repo: Path, comment: str) -> None:
    _write(repo, "tools/y.py", f"def f():\n    x = 1  # {comment}\n    return x\n")
    assert _scan(repo) == []


def test_an_id_in_a_docstring_is_refused(repo: Path) -> None:
    _write(repo, "tools/y.py", 'def f():\n    """See ABC-9 here."""\n    return 1\n')
    assert len(_scan(repo)) == 1


def test_an_id_in_a_docstring_survives_a_trailing_comment(repo: Path) -> None:
    _write(repo, "tools/y.py", 'def f():\n    """See ABC-9."""  # explanation\n    return 1\n')
    assert len(_scan(repo)) == 1


def test_an_id_in_a_returned_string_is_not_flagged(repo: Path) -> None:
    _write(repo, "tools/y.py", 'def f():\n    """Doc."""\n    return "ABC-9"\n')
    assert _scan(repo) == []


def test_noqa_excuses_a_genuine_literal(repo: Path) -> None:
    _write(repo, "tools/y.py", "def f():\n    x = 1  # color #1234 kept  # noqa\n    return x\n")
    assert _scan(repo) == []


def test_noqa_inside_a_string_does_not_excuse(repo: Path) -> None:
    _write(repo, "tools/y.py", 'def f():\n    x = "# noqa"  # see ABC-9\n    return x\n')
    assert len(_scan(repo)) == 1


# ------------------------------------------------------------- ratio


def _ratio_body(assignments: int) -> str:
    body = "    # a\n    # b\n    # c\n"
    body += "".join(f"    x{i} = {i}\n" for i in range(assignments))
    return f"def f():\n{body}    return 1\n"


def test_a_ratio_over_the_floor_is_refused(repo: Path) -> None:
    _write(repo, "tools/y.py", _ratio_body(8))  # def + 8 + return = 10 code, 3 comments
    assert _details(repo) == ["3 comment lines added vs 10 code lines added (over 25%)"]


def test_a_ratio_under_the_floor_passes(repo: Path) -> None:
    _write(repo, "tools/y.py", _ratio_body(1))  # 3 code lines, under the floor
    assert _scan(repo) == []


def test_the_pragma_excuses_a_private_docstring_from_the_ratio(repo: Path) -> None:
    doc = (
        "def _h():\n    # comments: derivation needed\n"
        '    """One.\n    Two.\n    Three.\n    Four.\n    Five.\n    """\n'
    )
    body = doc + "".join(f"    x{i} = {i}\n" for i in range(8)) + "    return 1\n"
    _write(repo, "tools/y.py", body)
    assert _scan(repo) == []


def test_a_parenthesized_docstring_does_not_count_as_code(repo: Path) -> None:
    # Its paren lines are docstring, not code: counted as code, they would lift
    # the denominator to 12, over the floor, and 4 comment lines would fail.
    head = 'def h():\n    (\n        "a "\n        "b"\n    )\n    # c1\n    # c2\n    # c3\n'
    body = (
        head
        + "    x0 = 0  # t\n"
        + "".join(f"    x{i} = {i}\n" for i in range(1, 6))
        + "    return 1\n"
    )
    _write(repo, "tools/y.py", body)
    assert _scan(repo) == []


# ------------------------------------------------------------- ERA001


@needs_ruff
def test_commented_out_code_is_refused(repo: Path) -> None:
    _write(repo, "tools/y.py", "x = 1\n# y = 2\n")
    assert _details(repo, era001=True) == ["commented-out code (ERA001)"]


@needs_ruff
def test_noqa_era001_is_honored(repo: Path) -> None:
    _write(repo, "tools/y.py", "x = 1\n# y = 2  # noqa: ERA001\n")
    assert _scan(repo, era001=True) == []


@needs_ruff
def test_preexisting_commented_out_code_passes(repo: Path, git: Callable[..., str]) -> None:
    _commit_on_main(repo, git, "tools/x.py", "x = 1\n# y = 2\n")
    _write(repo, "tools/x.py", "x = 1\n# y = 2\nz = 3\n")
    assert _scan(repo, era001=True) == []


@needs_ruff
def test_the_pragma_line_is_not_commented_out_code(repo: Path) -> None:
    # A one-word reason parses as code to ERA001; the pragma line must be skipped.
    body = (
        "def f():\n    # comments: legacy\n"
        "    # one\n    # two\n    # three\n    # four\n    return 1\n"
    )
    _write(repo, "tools/y.py", body)
    assert _scan(repo, era001=True) == []


def test_without_ruff_the_era001_check_is_skipped_with_a_note(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Value: protects=a host without ruff can still run the gate; fails_when=a
    # missing ruff is a fault; why_new=hosts may not have ruff; seam=PATH
    monkeypatch.setattr(cg.shutil, "which", lambda _name: None)
    _write(repo, "tools/y.py", "x = 1\n# y = 2\n")
    assert cg.main(["--base", "main"]) == 0
    err = capsys.readouterr().err
    assert "ruff is not on PATH" in err
    assert len(err.strip().splitlines()) == 1


def test_a_failing_ruff_is_a_fault(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "bin" / "ruff"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 2\n", encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    _write(repo, "tools/y.py", "x = 1\n")
    assert cg.main(["--base", "main"]) == 2


def test_parse_era001_rejects_bad_output() -> None:
    for stdout, code in [("", 1), ("null", 1), ("[]", 1), ("[{}]", 1)]:
        with pytest.raises(cg.CommentsError):
            cg._parse_era001(stdout, code, "y.py")
    with pytest.raises(cg.CommentsError):
        cg._parse_era001('[{"code": "ERA001", "location": {}}]', 1, "y.py")


def test_parse_era001_reads_rows_and_clean_output() -> None:
    assert cg._parse_era001("[]", 0, "y.py") == []
    payload = '[{"code": "ERA001", "location": {"row": 4, "column": 1}}]'
    assert cg._parse_era001(payload, 1, "y.py") == [4]


# ------------------------------------------------------------- policy


def test_without_a_policy_every_python_file_is_gated(repo: Path) -> None:
    _write(repo, "anywhere/deep/y.py", BLOCK4)
    _write(repo, "notes.txt", "# one\n# two\n# three\n# four\n")
    assert [v.path for v in _scan(repo)] == ["anywhere/deep/y.py"]


def test_include_and_exclude_narrow_the_scope(repo: Path) -> None:
    policy = {"include": ["tools/"], "exclude": ["tools/fixtures/"]}
    _write(repo, cg.POLICY_PATH, json.dumps(policy))
    _write(repo, "tools/y.py", BLOCK4)
    _write(repo, "tools/fixtures/z.py", BLOCK4)
    _write(repo, "docs/note.py", BLOCK4)
    assert [v.path for v in _scan(repo)] == ["tools/y.py"]


def test_the_policy_replaces_the_id_patterns(repo: Path) -> None:
    _write(repo, cg.POLICY_PATH, json.dumps({"ids": [r"\bTKT\d+\b"]}))
    _write(repo, "tools/y.py", "x = 1  # see ABC-9\ny = 2  # see TKT42\n")
    assert [v.line for v in _scan(repo)] == [2]


def test_an_empty_id_list_turns_the_ban_off(repo: Path) -> None:
    _write(repo, cg.POLICY_PATH, json.dumps({"ids": []}))
    _write(repo, "tools/y.py", "x = 1  # see ABC-9\n")
    assert _scan(repo) == []


def test_head_mode_reads_the_policy_at_head(repo: Path, git: Callable[..., str]) -> None:
    _write(repo, "vendor/y.py", BLOCK4)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "vendor")
    head = git(repo, "rev-parse", "HEAD").strip()
    assert len(cg.scan_range(repo, "main", head, era001=False)) == 1
    _write(repo, cg.POLICY_PATH, json.dumps({"exclude": ["vendor/"]}))
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "policy")
    head = git(repo, "rev-parse", "HEAD").strip()
    assert cg.scan_range(repo, "main", head, era001=False) == []


@pytest.mark.parametrize(
    "policy",
    [
        "{not json",
        "[]",
        '{"include": "tools/"}',
        '{"exclude": ["tools"]}',
        '{"exclude": ["../x/"]}',
        '{"include": ["t*/"]}',
        '{"ids": "ABC"}',
        '{"ids": [""]}',
        '{"ids": ["("]}',
    ],
)
def test_a_broken_policy_is_a_fault(repo: Path, policy: str) -> None:
    _write(repo, cg.POLICY_PATH, policy)
    _write(repo, "tools/y.py", "x = 1\n")
    assert cg.main(["--base", "main"]) == 2


# ------------------------------------------------------------- faults and git


def test_invalid_python_is_a_fault(repo: Path) -> None:
    _write(repo, "tools/y.py", "def broken(:\n")
    assert cg.main(["--base", "main"]) == 2


def test_an_unreadable_file_is_a_fault(repo: Path) -> None:
    (repo / "tools/y.py").write_bytes(b"\xff\xfe not utf-8\n")
    assert cg.main(["--base", "main"]) == 2


def test_a_deleted_file_is_skipped(repo: Path, git: Callable[..., str]) -> None:
    _commit_on_main(repo, git, "tools/x.py", BLOCK4)
    (repo / "tools/x.py").unlink()
    assert _scan(repo) == []


def test_a_bad_base_ref_is_a_fault(repo: Path) -> None:
    assert cg.main(["--base", "no-such-ref"]) == 2


def test_outside_a_repository_is_a_fault() -> None:
    # The autouse fixture puts the test in an empty directory, not a repository.
    assert cg.main([]) == 2


def test_the_message_names_location_rule_and_fix() -> None:
    msg = cg.Violation("tools/y.py", 3, "comment block of 4 lines (max 3)", "cut it").message()
    assert msg == "tools/y.py:3: comments: comment block of 4 lines (max 3); cut it."


def test_gate_reads_committed_lines_up_to_head(repo: Path, git: Callable[..., str]) -> None:
    _write(repo, "tools/y.py", BLOCK4)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "y")
    _write(repo, "tools/draft.py", BLOCK4)  # untracked: not part of HEAD
    assert [v.path for v in cg.gate(repo, "main")] == ["tools/y.py"]


def test_the_launcher_runs_the_rule_in_a_host_repository(repo: Path) -> None:
    # Value: protects=a stage calls the rule by name from any host repo;
    # fails_when=bin/loop-comments cannot find core; why_new=the port; seam=PATH
    launcher = PLUGIN / "bin" / "loop-comments"
    clean = subprocess.run([launcher, "--base", "main"], cwd=repo, capture_output=True, text=True)
    assert clean.returncode == 0, clean.stderr
    _write(repo, "tools/y.py", BLOCK4)
    dirty = subprocess.run([launcher, "--base", "main"], cwd=repo, capture_output=True, text=True)
    assert dirty.returncode == 1
    assert "tools/y.py:2: comments:" in dirty.stderr
