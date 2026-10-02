"""The complexity ratchet.

The gate keeps new code simple without failing a host's existing code. Each test
names the failure it prevents: a new function over the bar is refused, a listed
function that grew is refused, the policy file cannot be edited to launder a
failure, and malformed input exits 2 rather than passing or crashing.
"""

from __future__ import annotations

import ast
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from core import complexity_gate as cg

PLUGIN = Path(__file__).resolve().parent.parent
V = cg.POLICY_VERSION

CLEAN = "def f(x):\n    return x + 1\n"
# Twelve ifs: cyclomatic 13, length 24.
CC_OVER = "def big(x):\n" + "".join(f"    if x == {i}:\n        return {i}\n" for i in range(12))


def _func(source: str, key: str = "pkg/x.py:f") -> cg.Func:
    return cg.measure(source, "pkg/x.py")[key]


# ------------------------------------------------------------- cyclomatic

CC_CASES = [
    ("def f(x):\n    return x\n", 1),
    # The documented example: three ifs (one an elif) and one `and`, 1 + 3 + 1.
    (
        "def f(a, b):\n    if a and b:\n        return 1\n    elif a:\n        return 2\n"
        "    if b:\n        return 3\n    return 4\n",
        5,
    ),
    ("def f(x):\n    return 1 if x else 2\n", 2),
    ("def f(x):\n    assert x\n", 2),
    ("def f(x):\n    assert x and (1 if x else 2)\n", 2),
    ("def f(xs):\n    for x in xs:\n        pass\n    else:\n        pass\n", 3),
    ("async def f(xs):\n    async for x in xs:\n        pass\n", 2),
    ("def f(x):\n    while x:\n        x -= 1\n", 2),
    (
        "def f():\n    try:\n        pass\n    except ValueError:\n        pass\n"
        "    except KeyError:\n        pass\n    else:\n        pass\n    finally:\n        pass\n",
        4,
    ),
    ("def f():\n    try:\n        pass\n    except* ValueError:\n        pass\n", 2),
    ("def f(a, b, c):\n    return a or b or c\n", 3),
    ("def f(a, b, c):\n    return a and b or c\n", 3),
    ("def f(xs):\n    return [x for x in xs if x if x > 1]\n", 4),
    (
        "def f(x):\n    match x:\n        case 1:\n            pass\n        case 2:\n"
        "            pass\n        case _:\n            pass\n",
        3,
    ),
    (
        "def f(x):\n    match x:\n        case 1:\n            pass\n"
        "        case 2:\n            pass\n",
        3,
    ),
    ("def f(x):\n    with x:\n        return x\n", 1),
    ("def f(x):\n    def g(y):\n        if y:\n            return 1\n    return g\n", 1),
    ("def f(x):\n    class C:\n        y = 1 if x else 2\n    return C\n", 1),
    ("def f(x):\n    return lambda y: 1 if y else 2\n", 2),
    ("@d(1 if a else 2)\ndef f(x=1 if a else 2):\n    return x\n", 1),
]


@pytest.mark.parametrize(("source", "expected"), CC_CASES)
def test_cyclomatic_follows_the_documented_rules(source: str, expected: int) -> None:
    # Value: protects=the numbers a baseline records; fails_when=a counting rule drifts
    # from the docstring; why_new=the metric is computed in-house now; seam=none
    assert _func(source).cyclomatic == expected


def test_metrics_of_a_simple_function() -> None:
    func = _func(CLEAN)
    assert (func.cyclomatic, func.length) == (1, 1)


def test_the_over_bar_sample_has_the_numbers_the_tests_rely_on() -> None:
    func = _func(CC_OVER, "pkg/x.py:big")
    assert (func.cyclomatic, func.length) == (13, 24)


# ------------------------------------------------------------- length


def _length(source: str, key: str = "pkg/x.py:f") -> int:
    return _func(source, key).length


def test_length_ignores_comments_and_blank_lines() -> None:
    bare = "def f(x):\n    return x\n"
    padded = "def f(x):\n    # a long rationale\n\n    # more of it\n    return x\n"
    assert _length(padded) == _length(bare)


def test_length_ignores_this_functions_docstring() -> None:
    documented = 'def f(x):\n    """Why this exists."""\n    return x\n'
    assert _length(documented) == _length("def f(x):\n    return x\n")


def test_nested_docstring_does_not_buy_length() -> None:
    with_doc = (
        "def outer(x):\n"
        "    def inner(y):\n"
        '        """inner docs."""\n'
        "        return y\n"
        "    return inner(x)\n"
    )
    without = with_doc.replace('        """inner docs."""\n', "")
    assert _length(with_doc, "pkg/x.py:outer") == _length(without, "pkg/x.py:outer")


def test_inline_docstring_does_not_lower_length() -> None:
    assert _length('def f(): "doc"; return 1\n') == _length("def f(): return 1\n")


# ------------------------------------------------------------- keys


def test_method_and_property_keys_do_not_collide() -> None:
    source = (
        "class C:\n"
        "    @property\n"
        "    def v(self):\n        return self._v\n"
        "    @v.setter\n"
        "    def v(self, x):\n        self._v = x\n"
    )
    assert {"pkg/x.py:C.v", "pkg/x.py:C.v@setter"} <= set(cg.measure(source, "pkg/x.py"))


def test_duplicate_key_is_an_error() -> None:
    source = "class C:\n    def m(self):\n        return 1\n    def m(self):\n        return 2\n"
    with pytest.raises(cg.GateError):
        cg.measure(source, "pkg/x.py")


def test_function_under_an_if_block_is_measured() -> None:
    source = "import sys\nif sys.version_info:\n    def cond(x):\n        return x\n"
    assert "pkg/x.py:cond" in cg.measure(source, "pkg/x.py")


def test_nested_class_method_is_measured() -> None:
    # The earlier tool's analyzers could not see this and failed closed; the
    # in-house metric measures it under its dotted key.
    source = "class C:\n    class D:\n        def deep(self, x):\n            return x\n"
    assert _func(source, "pkg/x.py:C.D.deep").cyclomatic == 1


def test_overload_stubs_are_skipped_not_collided() -> None:
    source = (
        "from typing import overload\n"
        "class C:\n"
        "    @overload\n    def f(self, x: int) -> int: ...\n"
        "    @overload\n    def f(self, x: str) -> str: ...\n"
        "    def f(self, x):\n        return x\n"
    )
    assert list(cg.measure(source, "pkg/x.py")).count("pkg/x.py:C.f") == 1


def test_syntax_error_is_a_fault() -> None:
    with pytest.raises(cg.GateError):
        cg.measure("def broken(:\n", "pkg/x.py")


# ---------------------------------------------------------- diff parsing


def test_hunk_lines_reads_new_side() -> None:
    assert cg.hunk_lines("@@ -1,0 +2,3 @@\n+a\n+b\n+c\n") == {2, 3, 4}


def test_hunk_lines_deletion_marks_the_survivor() -> None:
    assert cg.hunk_lines("@@ -5,2 +4,0 @@\n") == {4, 5}


def test_added_content_cannot_impersonate_a_hunk_header() -> None:
    assert cg.hunk_lines("@@ -1,0 +1,1 @@\n+@@ -9 +9 @@\n") == {1}


def test_parse_name_status_returns_new_path_for_a_rename() -> None:
    z = "A\0pkg/new.py\0R100\0pkg/old.py\0pkg/renamed.py\0"
    assert cg.parse_name_status(z) == [("A", "pkg/new.py"), ("R100", "pkg/renamed.py")]


# ------------------------------------------------------------- settings


@pytest.mark.parametrize(
    ("path", "is_test"),
    [("tests/a.py", True), ("pkg/test_a.py", True), ("pkg/a.py", False), ("a_tests/x.py", False)],
)
def test_default_test_files(path: str, is_test: bool) -> None:
    assert cg.Settings().is_test(path) is is_test


@pytest.mark.parametrize(
    "data",
    [
        {"bars": {"cyclomatic": 10}},
        {"bars": {"cyclomatic": 10, "length": 0}},
        {"bars": {"cyclomatic": True, "length": 60}},
        {"scan": ["pkg"]},
        {"scan": ["../pkg/"]},
        {"scan": "pkg/"},
        {"tests": ["a/b.py"]},
        {"tests": [""]},
    ],
)
def test_bad_settings_are_a_fault(data: dict) -> None:
    with pytest.raises(cg.GateError):
        cg.parse_settings(data, "<t>")


def test_forward_migration_requires_plain_ints() -> None:
    assert cg._is_forward_migration(V - 1, V) is True
    assert cg._is_forward_migration(True, V) is False
    assert cg._is_forward_migration(V - 1, float(V)) is False


def test_base_revision_fails_loud_when_unresolvable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(*_a, **_k):
        return subprocess.CompletedProcess([], 128, b"", b"fatal: no merge base")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(cg.GateError):
        cg.base_revision(tmp_path, "origin/main", None)


# ------------------------------------------------------------- a real repository


@pytest.fixture
def repo(
    make_repo: Callable[[Path], Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """A host repository, the current directory, with one commit on main."""
    root = make_repo(tmp_path / "host")
    monkeypatch.chdir(root)
    return root


def _write(repo: Path, relpath: str, text: str) -> None:
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(git: Callable[..., str], repo: Path, message: str = "c") -> str:
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").strip()


def _dict(cc: int, length: int) -> dict:
    return {"cyclomatic": cc, "length": length}


def _baseline(functions: dict | None = None, policy: object = V, **settings: object) -> str:
    data = {"policy_version": policy, **settings, "functions": functions or {}}
    return json.dumps(data) + "\n"


def _set_baseline(repo: Path, functions: dict | None = None, **kw: object) -> None:
    _write(repo, cg.BASELINE_PATH, _baseline(functions, **kw))


@pytest.fixture
def base_with(repo: Path, git: Callable[..., str]) -> Callable[..., str]:
    """Commit pkg/x.py and a baseline; return the commit as the base."""

    def build(code: str = CLEAN, functions: dict | None = None, **kw: object) -> str:
        _write(repo, "pkg/x.py", code)
        _set_baseline(repo, functions, **kw)
        return _commit(git, repo, "base")

    return build


def _run(base: str, *extra: str) -> int:
    return cg.main(["--base", base, *extra])


def _wide(statements: int, comments: int) -> str:
    """A branch-free function: `statements` assignments and a return, padded with
    `comments` comment lines. Cyclomatic 1, so length is the only metric in play."""
    body = "".join(f"    # note {i}\n" for i in range(comments))
    body += "".join(f"    x{i} = {i}\n" for i in range(statements))
    return f"def wide(x):\n{body}    return x\n"


def _grow(source: str) -> str:
    """One more branch in `big`: cyclomatic up by one."""
    return source.rstrip("\n") + "\n    if x == 99:\n        return 99\n"


# ------------------------------------------------------------ the ratchet


def test_clean_new_function_passes(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with()
    _write(repo, "pkg/y.py", CLEAN)
    assert _run(base) == 0


def test_new_function_over_the_bar_is_refused(
    repo: Path, base_with: Callable[..., str], capsys: pytest.CaptureFixture[str]
) -> None:
    base = base_with()
    _write(repo, "pkg/y.py", CC_OVER)  # never git-added: an untracked file is seen
    assert _run(base) == 1
    assert "pkg/y.py:big:1: cyclomatic 13 > 10 (bar)" in capsys.readouterr().err


def test_baseline_function_that_grows_is_refused(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with(CC_OVER, {"pkg/x.py:big": _dict(13, 24)})
    _write(repo, "pkg/x.py", _grow(CC_OVER))
    assert _run(base) == 1


def test_baseline_function_at_its_number_passes(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with(CC_OVER, {"pkg/x.py:big": _dict(13, 24)})
    touched = CC_OVER.replace("def big(x):\n", "def big(x):\n    # touched inside\n", 1)
    _write(repo, "pkg/x.py", touched)
    assert _run(base) == 0


def test_incidental_touch_to_unlisted_old_violator_passes(
    repo: Path, base_with: Callable[..., str]
) -> None:
    base = base_with(CC_OVER)  # over the bar but not in the baseline
    _write(repo, "pkg/x.py", CC_OVER + "# touched\n")
    assert _run(base) == 0


def test_growth_hidden_by_deleted_comments_is_refused(
    repo: Path, base_with: Callable[..., str]
) -> None:
    # Two statements more, four comment lines fewer: a physical line count would
    # see a shrink; the statement count sees the growth.
    base_src = _wide(60, 6)
    base = base_with(base_src, {"pkg/x.py:wide": _dict(1, _length(base_src, "pkg/x.py:wide"))})
    _write(repo, "pkg/x.py", _wide(62, 2))
    assert _run(base) == 1


def test_a_test_file_is_exempt_from_length(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with()
    _write(repo, "tests/test_y.py", _wide(70, 0))
    assert _run(base) == 0
    _write(repo, "pkg/y.py", _wide(70, 0))
    assert _run(base) == 1


def test_head_mode_reads_the_commit_not_the_worktree(
    repo: Path, base_with: Callable[..., str], git: Callable[..., str]
) -> None:
    base = base_with()
    _write(repo, "pkg/y.py", CC_OVER)
    head = _commit(git, repo, "over")
    _write(repo, "pkg/y.py", CLEAN)  # fixed on disk only
    assert _run(base) == 0
    assert _run(base, "--head", head) == 1


# ---------------------------------------------------- baseline tampering


def test_raised_baseline_number_is_refused(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with(CC_OVER, {"pkg/x.py:big": _dict(13, 24)})
    _set_baseline(repo, {"pkg/x.py:big": _dict(99, 24)})
    assert _run(base) == 1


def test_added_baseline_key_is_refused(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with(functions={"pkg/x.py:f": _dict(11, 2)})
    _set_baseline(repo, {"pkg/x.py:f": _dict(11, 2), "pkg/x.py:g": _dict(11, 2)})
    assert _run(base) == 1


def test_removing_a_baseline_entry_is_allowed(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with(functions={"pkg/x.py:f": _dict(11, 2)})
    _set_baseline(repo, {})
    assert _run(base) == 0


def test_forward_migration_bypasses_tampering_check(
    repo: Path, base_with: Callable[..., str]
) -> None:
    base = base_with(CC_OVER, {"pkg/x.py:big": _dict(13, 5)}, policy=V - 1)
    _set_baseline(repo, {"pkg/x.py:big": _dict(99, 5)})
    assert _run(base) == 0


def test_mismatched_version_does_not_launder_a_new_entry(
    repo: Path, base_with: Callable[..., str]
) -> None:
    base = base_with(CC_OVER)
    _set_baseline(repo, {"pkg/x.py:big": _dict(13, 5)}, policy=V + 1)
    assert _run(base) == 1


def test_unauthorised_version_change_is_refused(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with()
    _set_baseline(repo, policy=V + 1)
    assert _run(base) == 1


def test_base_ahead_is_not_a_migration(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with(CC_OVER, policy=V + 1)
    _set_baseline(repo, {"pkg/x.py:big": _dict(13, 5)})
    assert _run(base) == 1


def test_version_type_change_is_refused(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with()
    _write(repo, cg.BASELINE_PATH, f'{{"policy_version": {V}.0, "functions": {{}}}}\n')
    assert _run(base) == 1


def test_an_older_tools_baseline_is_refused_not_misread(
    repo: Path, base_with: Callable[..., str], capsys: pytest.CaptureFixture[str]
) -> None:
    # Value: protects=a radon-era baseline is never read as this tool's numbers;
    # fails_when=a version 2 file is compared entry by entry; why_new=the metric
    # changed in the port; seam=policy_version
    old = {"pkg/x.py:big": {"cyclomatic": 13, "cognitive": 12, "length": 24}}
    base = base_with(CC_OVER, old, policy=2)
    assert _run(base) == 2
    assert "--update-baseline" in capsys.readouterr().err


@pytest.mark.parametrize(
    "settings",
    [
        {"bars": {"cyclomatic": 11, "length": 60}},
        {"scan": ["pkg/"]},
        {"tests": ["tests/", "test_*.py", "pkg/"]},
    ],
)
def test_a_loosened_setting_is_refused(
    repo: Path, base_with: Callable[..., str], settings: dict
) -> None:
    base = base_with()
    _set_baseline(repo, **settings)
    assert _run(base) == 1


def test_a_lowered_bar_applies(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with()
    _set_baseline(repo, bars={"cyclomatic": 1, "length": 60})
    _write(repo, "pkg/y.py", "def g(x):\n    if x:\n        return 1\n    return 2\n")
    assert _run(base) == 1


def test_scan_limits_what_is_gated(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with(scan=["pkg/"])
    _write(repo, "other/y.py", CC_OVER)
    assert _run(base) == 0
    _write(repo, "pkg/y.py", CC_OVER)
    assert _run(base) == 1


# --------------------------------------------------------- faults


def test_deleted_file_does_not_crash(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with(CC_OVER, {"pkg/x.py:big": _dict(13, 24)})
    (repo / "pkg/x.py").unlink()
    assert _run(base) == 0


def test_invalid_python_exits_two(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with()
    _write(repo, "pkg/y.py", "def broken(:\n")
    assert _run(base) == 2


def test_non_utf8_file_exits_two(repo: Path, base_with: Callable[..., str]) -> None:
    base = base_with()
    (repo / "pkg/y.py").write_bytes(b"\xff\xfe not utf-8\n")
    assert _run(base) == 2


@pytest.mark.parametrize("text", ["{not json", '{"functions": {"a": 1, "a": 2}}', "[]"])
def test_malformed_baseline_exits_two(repo: Path, base_with: Callable[..., str], text: str) -> None:
    base = base_with()
    _write(repo, cg.BASELINE_PATH, text)
    assert _run(base) == 2


def test_bad_base_ref_exits_two(base_with: Callable[..., str]) -> None:
    base_with()
    assert _run("no-such-ref") == 2


def test_main_outside_a_repository_is_a_fault() -> None:
    # The autouse fixture puts the test in an empty directory, not a repository.
    assert cg.main([]) == 2


# ------------------------------------------------------------ regeneration


def test_update_baseline_records_every_over_bar_function(
    repo: Path, git: Callable[..., str]
) -> None:
    _write(repo, "pkg/x.py", CC_OVER + "\n\n" + CLEAN)
    _commit(git, repo)
    assert cg.main(["--update-baseline"]) == 0
    data = json.loads((repo / cg.BASELINE_PATH).read_text(encoding="utf-8"))
    assert data["policy_version"] == V
    assert data["functions"] == {"pkg/x.py:big": _dict(13, 24)}


def test_update_baseline_keeps_the_settings(repo: Path) -> None:
    _write(repo, "pkg/x.py", CC_OVER)
    _set_baseline(repo, {"pkg/x.py:big": _dict(13, 24)}, scan=["pkg/"])
    assert cg.update_baseline(repo) == 0
    assert json.loads((repo / cg.BASELINE_PATH).read_text(encoding="utf-8"))["scan"] == ["pkg/"]


def test_policy_version_bump_authorises_a_wholesale_rebaseline(repo: Path) -> None:
    _write(repo, "pkg/x.py", CC_OVER)
    understated = {"pkg/x.py:big": _dict(11, 5)}
    _set_baseline(repo, understated)  # same version: raising 11 to 13 is refused
    assert cg.update_baseline(repo) == 1
    _set_baseline(repo, understated, policy=V - 1)  # an older version: regenerated
    assert cg.update_baseline(repo) == 0


def test_regen_of_an_empty_current_baseline_cannot_add(repo: Path) -> None:
    # An empty file at the tool's own version is not a bootstrap: a regen may
    # not slip a new over-bar function in.
    _write(repo, "pkg/x.py", CC_OVER)
    _set_baseline(repo, {})
    assert cg.update_baseline(repo) == 1


def test_baseline_ahead_of_tool_refuses_regen(repo: Path) -> None:
    _write(repo, "pkg/x.py", CC_OVER)
    _set_baseline(repo, policy=V + 1)
    assert cg.update_baseline(repo) == 1


# ------------------------------------------------------------ the loop and the plugin


def test_gate_reads_the_committed_branch(
    repo: Path, git: Callable[..., str], base_with: Callable[..., str]
) -> None:
    base_with()
    git(repo, "checkout", "-q", "-b", "feature")
    _write(repo, "pkg/y.py", CC_OVER)
    _commit(git, repo, "over")
    _write(repo, "pkg/z.py", CC_OVER)  # uncommitted: the stage has not ended with it
    found = cg.gate(repo, "main")
    assert [f.message() for f in found] == ["pkg/y.py:big:1: cyclomatic 13 > 10 (bar)"]


def test_gate_reports_a_tampered_baseline(
    repo: Path, git: Callable[..., str], base_with: Callable[..., str]
) -> None:
    base_with(CC_OVER, {"pkg/x.py:big": _dict(13, 24)})
    git(repo, "checkout", "-q", "-b", "feature")
    _set_baseline(repo, {"pkg/x.py:big": _dict(14, 24)})
    _commit(git, repo, "raise")
    assert [type(f) for f in cg.gate(repo, "main")] == [cg.BaselineFault]


def test_the_launcher_runs_the_ratchet_in_a_host_repository(
    repo: Path, base_with: Callable[..., str]
) -> None:
    # Value: protects=a stage or CI calls the ratchet by name from any host repo;
    # fails_when=bin/loop-complexity cannot find core; why_new=the port; seam=PATH
    base_with()
    launcher = PLUGIN / "bin" / "loop-complexity"
    clean = subprocess.run([launcher, "--base", "main"], cwd=repo, capture_output=True, text=True)
    assert clean.returncode == 0, clean.stderr
    _write(repo, "pkg/y.py", CC_OVER)
    dirty = subprocess.run([launcher, "--base", "main"], cwd=repo, capture_output=True, text=True)
    assert dirty.returncode == 1
    assert "pkg/y.py:big" in dirty.stderr


def test_the_launcher_parses_on_python_3_8() -> None:
    source = (PLUGIN / "bin" / "loop-complexity").read_text(encoding="utf-8")
    ast.parse(source, feature_version=(3, 8))
