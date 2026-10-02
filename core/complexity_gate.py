"""The complexity ratchet: new and changed functions must be simple.

Agent-written code trends more complex than expert code. An absolute gate would
fail a host's existing code on day one. A ratchet does not: nothing may get
worse, and new code must be clean.

WHAT IT CHECKS. Every function added or changed since a base revision, in the
Python files the policy scans (every tracked `*.py` by default). Two metrics per
function:

  * cyclomatic complexity, bar 10;
  * length in executable statements, bar 60. Test files are exempt, because
    table-driven tests are legitimately long. Length counts statements off the
    parsed syntax tree, so comments, blank lines and docstrings do not count and
    explanation is free: a function cannot be paid down by deleting it.

CYCLOMATIC COMPLEXITY. Computed here with `ast`, following radon's counting
rules, because the plugin installs no packages. A function starts at 1 and adds:

  * 1 per `if`, `elif` and conditional expression (`a if c else b`);
  * 1 per `assert`, and nothing for the condition inside it (`assert a and b`
    adds 1, as in radon);
  * 1 per `for`, `async for` and `while`, plus 1 when the loop has an `else`;
  * 1 per `except` handler of a `try`, plus 1 for its `else`; `finally` adds
    nothing. An `except*` handler counts the same; radon 6 predates it and
    counts nothing there, the one place this metric departs from radon;
  * the operand count minus 1 per boolean operation (`a and b or c` is two
    operations, `and` with 2 operands and `or` with 2, so it adds 2);
  * 1 per comprehension `for` clause, plus 1 per `if` in it;
  * 1 per `case` of a `match`, minus 1 when a case is a bare capture or `_`.

`with`, `return`, `break`, `continue`, a lambda itself, decorators and default
arguments add nothing. A nested `def` or `class` is its own scope: nothing in
its body counts toward the enclosing function (radon's rule too).

The cognitive metric the earlier tool also gated is dropped. It came from a
third-party analyzer with no small, faithful stdlib form, and a metric the gate
cannot reproduce exactly is one it cannot ratchet on.

THE POLICY. One JSON file in the host repository, by default
`.complexity-baseline.json` at its root, `--baseline` to name another. It holds
the settings and the baseline:

  * `bars`: `{"cyclomatic": 10, "length": 60}` by default;
  * `scan`: directory prefixes to gate, each ending in `/`; absent means every
    Python file;
  * `tests`: which files are tests (length-exempt). An entry ending in `/` is a
    directory prefix; any other entry is a file-name pattern. The default is
    `["tests/", "test_*.py"]`;
  * `functions`: every pre-existing function over a bar, with its numbers.

THE RATCHET. A listed function may not grow past its recorded number. An
unlisted function must pass the bars, unless it already existed at the base
revision with the same value (an incidental touch to old code does not fail).
The file itself is add-only-by-removal: the gate reads it at the base revision
and refuses an added key, a raised number, a raised bar, a narrowed `scan` or a
widened `tests`, so an agent cannot launder a failure by editing the file. Only
removing an entry or tightening a setting is allowed. A file absent at the base
is a one-time bootstrap.

POLICY VERSION. The file carries `policy_version`. A bump is the one authorised
wholesale re-baseline: a reviewed file at this tool's version over a base at an
older one is not checked entry by entry. Every other change to the field is
refused. A file from the older tool (version 2 or below, whose numbers came from
radon and complexipy) is refused with a message to regenerate it, never read
with the wrong meaning.

WHAT IT DOES NOT SEE. A nested function (a `def` inside a `def`) is not gated
on its own; its statements count toward the enclosing function's length, and
its branches toward nothing. Overload stubs are skipped. Files outside `scan`
are not gated.

MODES. Default (worktree): compare the base revision to the working tree, and
count every untracked, non-ignored Python file as new. `--head <sha>`: compare
the base revision to an explicit commit, for CI, which never trusts the working
tree; the policy file is read at that commit too.

FAIL CLOSED. An unparsable or non-UTF-8 file, a malformed policy, an
unresolvable base or a failing git call is a fault, never a silent pass.

Exit codes follow the shared tools contract: 0 clean, 1 refused (a function is
over the bar, grew, or the policy file was tampered with), 2 a usage or
environment fault.

  loop-complexity                    # gate the working tree against origin/main
  loop-complexity --head "$SHA"      # gate an explicit commit (CI)
  loop-complexity --update-baseline  # regenerate the baseline after a paydown
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from core.config import git_env

RULE_ID = "complexity"
BASELINE_PATH = ".complexity-baseline.json"
# 3: cyclomatic computed in-house, cognitive dropped. 2 and below were the older
# tool's radon and complexipy numbers, which this tool must not read.
POLICY_VERSION = 3

METRICS = ("cyclomatic", "length")
DEFAULT_BARS = {"cyclomatic": 10, "length": 60}
TEST_EXEMPT = ("length",)
DEFAULT_TESTS = ("tests/", "test_*.py")

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
# Nodes the cyclomatic walk does not enter: a nested scope, and an assert, whose
# condition radon does not read.
_OPAQUE = (*_SCOPES, ast.Assert)


class GateError(Exception):
    """A usage or environment fault. Maps to exit 2."""


# ------------------------------------------------------------- settings


@dataclass(frozen=True)
class Settings:
    """The bars, the files gated and the files counted as tests."""

    bars: tuple[tuple[str, int], ...] = tuple(DEFAULT_BARS.items())
    scan: tuple[str, ...] | None = None
    tests: tuple[str, ...] = DEFAULT_TESTS

    def bar(self, metric: str) -> int:
        return dict(self.bars)[metric]

    def scans(self, path: str) -> bool:
        if not path.endswith(".py"):
            return False
        return self.scan is None or any(path.startswith(p) for p in self.scan)

    def is_test(self, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return any(
            path.startswith(p) if p.endswith("/") else fnmatch.fnmatchcase(name, p)
            for p in self.tests
        )

    def bars_for(self, is_test: bool) -> dict[str, int]:
        return {m: b for m, b in self.bars if not (is_test and m in TEST_EXEMPT)}


def _is_exact_path(path: object) -> bool:
    """A repo-relative path: a non-empty string, no glob, not absolute, no `..`."""
    return (
        isinstance(path, str)
        and bool(path)
        and "*" not in path
        and not path.startswith("/")
        and ".." not in path.split("/")
    )


def _parse_bars(raw: object, label: str) -> tuple[tuple[str, int], ...]:
    if raw is None:
        return tuple(DEFAULT_BARS.items())
    ok = isinstance(raw, dict) and set(raw) == set(METRICS)
    if not ok or not all(type(v) is int and v > 0 for v in raw.values()):
        raise GateError(f"{label}: bars needs positive integer {', '.join(METRICS)}")
    return tuple((m, raw[m]) for m in METRICS)


def _parse_scan(raw: object, label: str) -> tuple[str, ...] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(_is_exact_path(p) and p.endswith("/") for p in raw):
        raise GateError(f"{label}: scan must list repo-relative directories ending in /")
    return tuple(raw)


def _is_test_entry(entry: object) -> bool:
    """A directory prefix ending in `/`, or a file-name pattern with no `/`."""
    if not isinstance(entry, str) or not entry:
        return False
    if entry.endswith("/"):
        return _is_exact_path(entry)
    return "/" not in entry


def _parse_tests(raw: object, label: str) -> tuple[str, ...]:
    if raw is None:
        return DEFAULT_TESTS
    if not isinstance(raw, list) or not all(_is_test_entry(e) for e in raw):
        raise GateError(f"{label}: tests must list directories ending in / or file-name patterns")
    return tuple(raw)


def parse_settings(data: dict, label: str) -> Settings:
    return Settings(
        _parse_bars(data.get("bars"), label),
        _parse_scan(data.get("scan"), label),
        _parse_tests(data.get("tests"), label),
    )


# --------------------------------------------------------------- measurement


@dataclass(frozen=True)
class Func:
    """One measured function: its stable key, its span and its numbers."""

    key: str
    def_line: int
    first_line: int
    end: int
    cyclomatic: int
    length: int

    def value(self, metric: str) -> int:
        return getattr(self, metric)

    def metrics(self) -> dict[str, int]:
        return {m: self.value(m) for m in METRICS}


def _decorator_target(dec: ast.expr) -> ast.expr:
    return dec.func if isinstance(dec, ast.Call) else dec


def _accessor_suffix(node: ast.AST) -> str:
    """Tell a property setter or deleter from its getter, so the two do not
    collide on one key."""
    for dec in getattr(node, "decorator_list", []):
        attr = getattr(_decorator_target(dec), "attr", None)
        if attr in ("setter", "deleter"):
            return f"@{attr}"
    return ""


def _is_overload(node: ast.AST) -> bool:
    """A @overload stub: a type-only declaration, and several share one name."""
    for dec in getattr(node, "decorator_list", []):
        target = _decorator_target(dec)
        if "overload" in (getattr(target, "id", None), getattr(target, "attr", None)):
            return True
    return False


def _is_docstring(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def statement_length(node: ast.AST) -> int:
    """A function's length in executable statements. Comments and blank lines
    are not in the tree, and every docstring (this function's and any nested def
    or class) is subtracted, so deleting explanation cannot pay the length down.
    Nested statements count toward the enclosing function."""
    stmts = [n for n in ast.walk(node) if isinstance(n, ast.stmt) and n is not node]
    docstrings = sum(
        1
        for scope in ast.walk(node)
        if isinstance(scope, _SCOPES) and scope.body and _is_docstring(scope.body[0])
    )
    return len(stmts) - docstrings


def _match_decisions(node: ast.Match) -> int:
    # radon's rule: a case whose pattern is a bare capture or `_` is the default.
    has_default = any(
        isinstance(c.pattern, ast.MatchAs) and c.pattern.pattern is None for c in node.cases
    )
    return max(0, len(node.cases) - has_default)


def _decisions(node: ast.AST) -> int:
    """What one node adds to cyclomatic complexity (see the module docstring)."""
    if isinstance(node, (ast.If, ast.IfExp, ast.Assert)):
        return 1
    if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
        return 1 + bool(node.orelse)
    if isinstance(node, (ast.Try, ast.TryStar)):
        return len(node.handlers) + bool(node.orelse)
    if isinstance(node, ast.BoolOp):
        return len(node.values) - 1
    if isinstance(node, ast.comprehension):
        return 1 + len(node.ifs)
    if isinstance(node, ast.Match):
        return _match_decisions(node)
    return 0


def cyclomatic(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """1 plus every decision in the body, not entering a nested def or class or
    an assert's condition. Decorators and default arguments are outside the body,
    so they do not count."""
    total = 1
    stack: list[ast.AST] = list(node.body)
    while stack:
        child = stack.pop()
        total += _decisions(child)
        if not isinstance(child, _OPAQUE):
            stack.extend(ast.iter_child_nodes(child))
    return total


def _make_func(node: ast.FunctionDef | ast.AsyncFunctionDef, prefix: str, relpath: str) -> Func:
    key = f"{relpath}:{prefix}{node.name}{_accessor_suffix(node)}"
    first = min([node.lineno, *(d.lineno for d in node.decorator_list)])
    end = node.end_lineno or node.lineno
    return Func(key, node.lineno, first, end, cyclomatic(node), statement_length(node))


def _collect(node: ast.AST, prefix: str, relpath: str, out: list[Func]) -> None:
    """Every function and method, including one under an if/try/with/loop block
    and a method of a nested class, but never one inside another function body.
    Overload stubs are skipped."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            _collect(child, f"{prefix}{child.name}.", relpath, out)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _is_overload(child):
                out.append(_make_func(child, prefix, relpath))
        else:
            _collect(child, prefix, relpath, out)


def measure(source: str, relpath: str) -> dict[str, Func]:
    """Every function in one file, keyed. A syntax error or a duplicate key is a
    fault (exit 2): the gate fails closed rather than pass a function unmeasured."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise GateError(f"{relpath}: cannot parse ({exc})") from exc
    funcs: list[Func] = []
    _collect(tree, "", relpath, funcs)
    out: dict[str, Func] = {}
    for func in funcs:
        if func.key in out:
            raise GateError(f"{relpath}: duplicate function key {func.key}")
        out[func.key] = func
    return out


# --------------------------------------------------------------------- git


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=root, env=git_env(), capture_output=True, check=False)


def _git(root: Path, *args: str) -> str:
    proc = _run_git(root, *args)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise GateError(f"git {' '.join(args)} failed: {err}")
    return proc.stdout.decode("utf-8", "surrogateescape")


def _decode(raw: bytes, label: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GateError(f"{label}: not UTF-8 ({exc})") from exc


def repo_root(cwd: Path) -> Path:
    """The top of the repository `cwd` is in. Outside one is a usage fault."""
    return Path(_git(cwd, "rev-parse", "--show-toplevel").strip())


def show(root: Path, rev: str, relpath: str) -> str | None:
    """The file at a revision, or None when it did not exist there. A path that
    exists but cannot be read is a fault, not a false 'absent'."""
    if _run_git(root, "cat-file", "-e", f"{rev}:{relpath}").returncode != 0:
        return None
    got = _run_git(root, "show", f"{rev}:{relpath}")
    if got.returncode != 0:
        err = got.stderr.decode("utf-8", "replace").strip()
        raise GateError(f"{rev}:{relpath}: exists but is unreadable ({err})")
    return _decode(got.stdout, f"{rev}:{relpath}")


def base_revision(root: Path, base_ref: str, head: str | None) -> str:
    """The merge base as a SHA. A missing ref fails loud, never a silent pass."""
    proc = _run_git(root, "merge-base", base_ref, head or "HEAD")
    sha = proc.stdout.decode("utf-8", "replace").strip()
    if proc.returncode != 0 or not sha:
        err = proc.stderr.decode("utf-8", "replace").strip() or "no output"
        raise GateError(
            f"cannot resolve merge-base of {base_ref} and {head or 'HEAD'} ({err}); "
            "fetch the base, do not skip the gate"
        )
    return sha


def _rev_range(base_rev: str, head: str | None) -> list[str]:
    return [base_rev, head] if head else [base_rev]


def parse_name_status(z: str) -> list[tuple[str, str]]:
    """(status, current path) per NUL record of `git diff --name-status -z`. A
    rename or copy carries old and new paths; the new path is returned."""
    fields = z.split("\0")
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(fields) and fields[i]:
        status = fields[i]
        if status.startswith(("R", "C")):
            out.append((status, fields[i + 2]))
            i += 3
        else:
            out.append((status, fields[i + 1]))
            i += 2
    return out


def hunk_lines(diff_text: str) -> set[int]:
    """New-side changed line numbers from one file's `-U0` diff. A pure deletion
    (new-side count 0) still marks the surviving line, so merging two functions by
    deleting a `def` is caught. Only a line that starts with `@@` is a header;
    body lines are prefixed, so added content cannot impersonate one."""
    lines: set[int] = set()
    for line in diff_text.splitlines():
        match = _HUNK_RE.match(line)
        if match:
            start, count = int(match.group(1)), int(match.group(2) or 1)
            lines.update(range(start, start + count) if count else (start, start + 1))
    return lines


# --------------------------------------------------------------- the tree


@dataclass(frozen=True)
class Tree:
    """The side of the diff being gated: the working tree, or the commit `head`."""

    root: Path
    head: str | None

    def read(self, relpath: str) -> str | None:
        if self.head is not None:
            return show(self.root, self.head, relpath)
        disk = self.root / relpath
        if not disk.is_file():
            return None
        return _decode(disk.read_bytes(), relpath)

    def changed(self, base_rev: str, settings: Settings) -> dict[str, set[int]]:
        """Changed line numbers per gated file that still exists."""
        out = _git(
            self.root,
            "-c",
            "core.quotepath=false",
            "diff",
            "--name-status",
            "-z",
            *_rev_range(base_rev, self.head),
        )
        changed: dict[str, set[int]] = {}
        for status, path in parse_name_status(out):
            if settings.scans(path) and not status.startswith("D"):
                changed[path] = self._hunks(base_rev, path)
        return changed

    def _hunks(self, base_rev: str, path: str) -> set[int]:
        out = _git(
            self.root,
            "-c",
            "core.quotepath=false",
            "diff",
            "-U0",
            "--no-ext-diff",
            "--no-textconv",
            *_rev_range(base_rev, self.head),
            "--",
            path,
        )
        return hunk_lines(out)

    def untracked(self, settings: Settings) -> list[str]:
        """New files not yet in the diff. Revision mode never reads the worktree."""
        if self.head is not None:
            return []
        out = _git(
            self.root,
            "-c",
            "core.quotepath=false",
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        return [p for p in out.split("\0") if settings.scans(p)]


@dataclass
class Scope:
    """Which functions changed, and the base revision to compare against.
    Every function of an untracked file counts as new."""

    changed: dict[str, set[int]]
    untracked: list[str]
    base_rev: str

    def files(self) -> list[str]:
        return list(self.changed) + [p for p in self.untracked if p not in self.changed]

    def in_scope(self, path: str, func: Func) -> bool:
        if path in self.untracked:
            return True
        return bool(
            self.changed.get(path, set()).intersection(range(func.first_line, func.end + 1))
        )


# --------------------------------------------------------------- baseline


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    out: dict[str, object] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key {key!r}")
        out[key] = value
    return out


def parse_baseline(text: str, label: str) -> dict:
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicate_keys)
    except ValueError as exc:
        raise GateError(f"{label}: malformed baseline ({exc})") from exc
    if not isinstance(data, dict):
        raise GateError(f"{label}: baseline must be a JSON object")
    return data


def load_baseline(tree: Tree, relpath: str) -> dict:
    """The policy file on the gated side, parsed. Absent is an empty baseline."""
    text = tree.read(relpath)
    return parse_baseline(text, relpath) if text is not None else {}


def baseline_functions(data: dict, label: str) -> dict[str, dict[str, int]]:
    """The validated `functions` map: every entry carries exactly the integer
    metrics, so a partial record cannot smuggle a metric back in."""
    funcs = data.get("functions", {})
    if not isinstance(funcs, dict):
        raise GateError(f"{label}: functions must be an object")
    for key, metrics in funcs.items():
        ok = isinstance(metrics, dict) and set(metrics) == set(METRICS)
        if not ok or not all(type(v) is int for v in metrics.values()):
            raise GateError(f"{label}: entry {key} needs integer {', '.join(METRICS)}")
    return funcs


def refuse_older_tool(data: dict, label: str) -> None:
    """A file from the older tool holds numbers this tool does not compute."""
    version = data.get("policy_version")
    if type(version) is int and version < POLICY_VERSION:
        raise GateError(
            f"{label}: policy_version {version} is from an older complexity tool whose "
            f"numbers came from radon and complexipy; regenerate it with "
            f"loop-complexity --update-baseline (policy_version {POLICY_VERSION})"
        )


def require_current(data: dict, label: str) -> None:
    """A present policy file must carry this tool's own version."""
    refuse_older_tool(data, label)
    version = data.get("policy_version")
    if data and not (type(version) is int and version == POLICY_VERSION):
        raise GateError(f"{label}: policy_version must be {POLICY_VERSION}, not {version!r}")


# --------------------------------------------------------- ratchet logic


def allowed_value(key: str, metric: str, bar: int, listed: dict, base: dict) -> int:
    """A function present at the base may keep its base value but not grow; a new
    function gets only the bar. The recorded baseline number is a fallback used
    only when the base value is unavailable (a renamed or stale entry), never
    added on top of it: that would let a paid-down function climb back."""
    if key in base:
        return max(bar, base[key][metric])
    if key in listed:
        return max(bar, listed[key][metric])
    return bar


@dataclass(frozen=True)
class Violation:
    key: str
    line: int
    metric: str
    value: int
    allowed: int
    listed: bool

    def message(self) -> str:
        source = "baseline" if self.listed else "bar"
        return f"{self.key}:{self.line}: {self.metric} {self.value} > {self.allowed} ({source})"


@dataclass(frozen=True)
class BaselineFault:
    """A refused edit to the policy file."""

    text: str

    def message(self) -> str:
        return self.text


Finding = Violation | BaselineFault


def check_function(
    func: Func, bars: dict[str, int], listed: dict, base: dict[str, Func]
) -> list[Violation]:
    base_numbers = {k: f.metrics() for k, f in base.items()}
    out: list[Violation] = []
    for metric, bar in bars.items():
        value = func.value(metric)
        allowed = allowed_value(func.key, metric, bar, listed, base_numbers)
        if value > allowed:
            out.append(
                Violation(func.key, func.def_line, metric, value, allowed, func.key in listed)
            )
    return out


def _base_funcs(root: Path, base_rev: str, path: str) -> dict[str, Func]:
    source = show(root, base_rev, path)
    return measure(source, path) if source is not None else {}


def scan(tree: Tree, scope: Scope, settings: Settings, listed: dict) -> list[Violation]:
    violations: list[Violation] = []
    for path in scope.files():
        source = tree.read(path)
        if source is None:
            continue  # deleted; its baseline entries fall away as allowed removals
        base = _base_funcs(tree.root, scope.base_rev, path)
        bars = settings.bars_for(settings.is_test(path))
        for func in measure(source, path).values():
            if scope.in_scope(path, func):
                violations.extend(check_function(func, bars, listed, base))
    return violations


# ---------------------------------------------------- baseline tampering


def _is_forward_migration(base_version: object, current_version: object) -> bool:
    """True only for the one authorised re-baseline: base strictly older than the
    tool, reviewed file at the tool's version. Any other version change is refused,
    so the exemption cannot be rearmed. `type(...) is int` rejects bool and float."""
    return (
        type(current_version) is int
        and current_version == POLICY_VERSION
        and type(base_version) is int
        and base_version < POLICY_VERSION
    )


def _version_changed(base_version: object, current_version: object) -> bool:
    """A type change (`3` to `3.0`) counts, so equality cannot hide an edit."""
    return current_version != base_version or type(current_version) is not type(base_version)


def _raised(key: str, current: dict[str, int], base: dict[str, int]) -> list[str]:
    return [
        f"baseline raises {key} {metric} {base[metric]} -> {value}; lower or remove only"
        for metric, value in current.items()
        if metric in base and value > base[metric]
    ]


def _loosened(base: Settings, current: Settings) -> list[str]:
    """A raised bar, a narrowed scan or a widened test set would let more through."""
    out = [
        f"baseline raises the {m} bar {base.bar(m)} -> {current.bar(m)}; lower only"
        for m in METRICS
        if current.bar(m) > base.bar(m)
    ]
    wider = current.scan is None or (base.scan is not None and set(base.scan) <= set(current.scan))
    if not wider:
        out.append("baseline scans less than at the base; scan may only gain entries")
    if not set(current.tests) <= set(base.tests):
        out.append("baseline counts more files as tests than at the base; tests may only shrink")
    return out


def check_tampering(base_data: dict | None, current: dict, label: str) -> list[str]:
    """Refuse an added key, a raised number or a loosened setting versus the file
    at the base revision. A file absent at the base is a one-time bootstrap; an
    empty-but-present file is not, so it cannot be a laundering step."""
    if base_data is None:
        return []
    base_version, current_version = base_data.get("policy_version"), current.get("policy_version")
    if _is_forward_migration(base_version, current_version):
        return []
    if _version_changed(base_version, current_version):
        return [
            f"baseline policy_version {base_version!r} -> {current_version!r} is not an "
            f"authorised migration; only a forward re-baseline to {POLICY_VERSION} may change it"
        ]
    faults = _loosened(parse_settings(base_data, "base"), parse_settings(current, label))
    base = baseline_functions(base_data, "base")
    for key, metrics in baseline_functions(current, label).items():
        if key not in base:
            faults.append(f"baseline adds {key}; the baseline is add-only-by-removal")
            continue
        faults.extend(_raised(key, metrics, base[key]))
    return faults


# ------------------------------------------------------------------ gate


def check(
    root: Path, base_ref: str, head: str | None, baseline: str = BASELINE_PATH
) -> list[Finding]:
    """Every refusal for the diff from the base to `head` (None: the working
    tree). A tampered policy file is reported alone: the functions cannot be
    judged against numbers that are not trusted. A fault raises `GateError`."""
    if not _is_exact_path(baseline):
        raise GateError(f"--baseline {baseline!r} must be a repo-relative path")
    tree = Tree(root, head)
    base_rev = base_revision(root, base_ref, head)
    current = load_baseline(tree, baseline)
    refuse_older_tool(current, baseline)
    base_text = show(root, base_rev, baseline)
    base_data = None if base_text is None else parse_baseline(base_text, f"base:{baseline}")
    faults = check_tampering(base_data, current, baseline)
    if faults:
        return [BaselineFault(f) for f in faults]
    require_current(current, baseline)
    settings = parse_settings(current, baseline)
    listed = baseline_functions(current, baseline)
    scope = Scope(tree.changed(base_rev, settings), tree.untracked(settings), base_rev)
    return list(scan(tree, scope, settings, listed))


def gate(root: Path, base_ref: str, baseline: str = BASELINE_PATH) -> list[Finding]:
    """What the loop checks before it lets a stage end: every function the branch
    adds or changes up to HEAD, and the policy file at HEAD. A fault raises
    `GateError`, so the caller fails closed."""
    return check(root, base_ref, "HEAD", baseline)


# ------------------------------------------------------------ baseline gen


def _all_python(root: Path, settings: Settings) -> list[str]:
    out = _git(
        root,
        "-c",
        "core.quotepath=false",
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    return sorted({p for p in out.split("\0") if settings.scans(p) and (root / p).is_file()})


def generate_functions(root: Path, settings: Settings) -> dict[str, dict[str, int]]:
    """Every function over a bar that applies to it, with all its numbers."""
    tree = Tree(root, None)
    functions: dict[str, dict[str, int]] = {}
    for path in _all_python(root, settings):
        bars = settings.bars_for(settings.is_test(path))
        for key, func in measure(tree.read(path) or "", path).items():
            if any(func.value(m) > bar for m, bar in bars.items()):
                functions[key] = func.metrics()
    return dict(sorted(functions.items()))


def _is_current(data: dict) -> bool:
    version = data.get("policy_version")
    return type(version) is int and version == POLICY_VERSION


def _regen_existing(prior: dict) -> dict | None:
    """The entries a regen must not exceed. Empty means a wholesale re-baseline
    (file absent or older than the tool); None means refuse (file ahead of the
    tool). An empty file at the tool's version stays strict via `_is_current`."""
    version = prior.get("policy_version")
    if type(version) is int and version == POLICY_VERSION:
        return baseline_functions(prior, "baseline")
    if version is None or (type(version) is int and version < POLICY_VERSION):
        return {}
    return None


def _regen_faults(fresh: dict, existing: dict) -> list[str]:
    out: list[str] = []
    for key, metrics in fresh.items():
        if key not in existing:
            out.append(f"{key} is a new over-bar function; add it in its own change, not via regen")
            continue
        out.extend(_raised(key, metrics, existing[key]))
    return out


def _settings_of(prior: dict) -> tuple[Settings, dict]:
    """The settings to regenerate under, and the raw keys to write back. An older
    tool's file carried no settings this tool reads."""
    if not _is_current(prior):
        return Settings(), {}
    kept = {k: prior[k] for k in ("bars", "scan", "tests") if k in prior}
    return parse_settings(prior, "baseline"), kept


def update_baseline(root: Path, baseline: str = BASELINE_PATH) -> int:
    prior = load_baseline(Tree(root, None), baseline)
    existing = _regen_existing(prior)
    if existing is None:
        print(
            "  baseline policy_version is ahead of the tool; refusing to downgrade.",
            file=sys.stderr,
        )
        return 1
    settings, kept = _settings_of(prior)
    fresh = generate_functions(root, settings)
    faults = _regen_faults(fresh, existing) if _is_current(prior) else []
    if faults:
        for fault in faults:
            print(f"  {fault}", file=sys.stderr)
        print("\nRegeneration may only lower or remove numbers, not raise or add.", file=sys.stderr)
        return 1
    data = {"policy_version": POLICY_VERSION, "measured": date.today().isoformat(), **kept}
    data["functions"] = fresh
    (root / baseline).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {baseline}: {len(fresh)} function(s) over a bar.")
    return 0


# ------------------------------------------------------------------ main


def _report(found: list[Finding]) -> None:
    for item in found:
        print(f"  {item.message()}", file=sys.stderr)
    print(
        "\nNew code has no baseline escape: reduce complexity; the baseline is "
        "add-only-by-removal. Move the new lines into a helper, or split the "
        "function; deleting comments or docstrings does not count. Then "
        "`loop-complexity --update-baseline`.",
        file=sys.stderr,
    )


def _run(args: argparse.Namespace) -> int:
    root = repo_root(Path.cwd())
    if args.update_baseline:
        return update_baseline(root, args.baseline)
    found = check(root, args.base, args.head, args.baseline)
    if found:
        _report(found)
        return 1
    print(f"{RULE_ID}: clean.")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loop-complexity",
        description="The complexity ratchet over the functions a diff adds or changes.",
    )
    parser.add_argument("--base", default="origin/main", help="base ref (default origin/main)")
    parser.add_argument("--head", default=None, help="explicit head commit; CI mode")
    parser.add_argument(
        "--baseline",
        default=BASELINE_PATH,
        help=f"repo-relative policy file (default {BASELINE_PATH})",
    )
    parser.add_argument(
        "--update-baseline", action="store_true", help="regenerate the baseline after a paydown"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args)
    except GateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
