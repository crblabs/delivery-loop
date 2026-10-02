"""The comment-hygiene rule: a ratchet on the diff.

A comment says why, in at most three lines. The code says what. This module is
the one place that decides what the mechanical half of that rule means, so every
enforcement path a host wires up agrees byte for byte. The semantic half (why,
not what) is a written convention a matcher cannot prove, so it is not enforced
here.

WHAT IT GATES. Five mechanical checks, each on the diff since the merge base:
  * a comment block longer than three lines;
  * a non-exempt docstring longer than three lines;
  * a ticket, issue or pull-request id inside a comment or docstring;
  * commented-out code (ruff ERA001), when ruff is on PATH;
  * comment lines added out of proportion to code lines added, per file.

BLOCK, NOT LINE. A block is a run of own-line comments at one indentation, or a
docstring. The trigger is added lines only: a block is checked only when the
diff touches one of its lines, so untouched code never fails and the repository
is not rewritten in bulk. But the length measured is the whole resulting block,
read from the head source, so appending one line to a three-line block is caught.

CLASSIFY WITH THE PARSER, NOT A REGEX. A hash inside a string literal is not a
comment, and a docstring is an AST node, not a hash line. The scan uses the
tokenizer for comments and the AST for docstrings, so string data never trips it.

EXEMPTIONS. A module, a class, or a public function or method may open with a
docstring of any length. A private helper docstring and every hash-comment block
are held to three lines. One escape valve, block-scoped: a standalone
`# comments: <reason>` line immediately above a block, at matching indentation,
excuses that block's length and drops its comment lines from the ratio. It does
not excuse the id ban, so its own reason text is still read for an id, but the
pragma line itself is never read as commented-out code. For a genuine id-shaped
literal in an ordinary comment, a `# noqa` on the line is the out.

THE POLICY. One optional JSON file in the host repository, by default
`.comments-policy.json` at its root, `--policy` to name another. Every key is
optional:
  * `include`: directory prefixes to gate. Absent, every Python file is gated.
  * `exclude`: directory prefixes to skip (captured fixtures, vendored code).
  * `ids`: the regular expressions that count as a ticket, issue or pull-request
    id, replacing the defaults. An empty list turns the id ban off.
Each prefix ends with `/`, has no glob and does not climb out of the repository.
The policy is read at the diff's head, so a change to it is visible in the same
pull request it takes effect in.

FAIL CLOSED. A broken policy, a file the tokenizer or the parser cannot read, or
a ruff run that cannot complete is a fault (exit 2), never a silent pass. A host
without ruff skips the commented-out code check and says so; it is not a fault.

Exit codes follow the shared tools contract: 0 clean, 1 a violation, 2 a usage
or environment fault.

  loop-comments                         # gate the working tree against origin/main
  loop-comments --head "$SHA"           # gate an explicit commit (CI)
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import re
import shutil
import subprocess
import sys
import tokenize
from dataclasses import dataclass, field
from pathlib import Path

from core import no_unicode_dash as nd

RULE_ID = "comments"
POLICY_PATH = ".comments-policy.json"

MAX_BLOCK = 3
RATIO_FLOOR = 10
RATIO_NUM, RATIO_DEN = 1, 4  # fail when RATIO_DEN * numerator > RATIO_NUM * denominator

RUFF_TIMEOUT = 60

_PRAGMA = re.compile(r"^#\s*comments:\s*\S")
_NOQA = re.compile(r"#\s*noqa(?![-\w])")

# Standard names shaped like a ticket key (UTF-8, SHA-256, RFC-3339) are not ids.
_STANDARDS = "UTF|UCS|SHA|ISO|IEC|AES|RFC|PEP|CVE|CWE|TLS|HTTP"
DEFAULT_IDS = (
    rf"\b(?!(?:{_STANDARDS})\d*-)[A-Z][A-Z0-9]+-\d+\b",
    r"https?://[^\s)]+/(?:issues|pull|pulls|merge_requests)/\d+",
    r"(?<![\w#])#\d+\b",
)

_FIX_BLOCK = "cut it to why-only, or add '# comments: <reason>' on the line above"
_FIX_DOC = "shorten it, or expose the entry (a public def, class or module is exempt)"
_FIX_ID = (
    "move the id to the commit message, or add '# noqa' for a genuine literal "
    f"(the id patterns live in {POLICY_PATH})"
)
_FIX_ERA = "delete the dead code, or add '# noqa: ERA001' if it is deliberate"
_FIX_RATIO = "cut restating comments, or add '# comments: <reason>' above a dense block"


class CommentsError(Exception):
    """A usage or environment fault. Maps to exit 2."""


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    detail: str
    fix: str

    def message(self) -> str:
        return f"{self.path}:{self.line}: {RULE_ID}: {self.detail}; {self.fix}."


@dataclass
class Analysis:
    own_comment: dict[int, int] = field(default_factory=dict)
    pragma_lines: dict[int, int] = field(default_factory=dict)
    trailing: set[int] = field(default_factory=set)
    comment_text: dict[int, str] = field(default_factory=dict)
    code_lines: set[int] = field(default_factory=set)
    doc_spans: list[tuple[int, int, bool, int]] = field(default_factory=list)
    doc_lines: set[int] = field(default_factory=set)
    doc_text: dict[int, str] = field(default_factory=dict)
    source_lines: list[str] = field(default_factory=list)


# ------------------------------------------------------------- policy


@dataclass(frozen=True)
class Policy:
    """Which Python files the rule reads, and what counts as an id."""

    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    ids: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in DEFAULT_IDS)

    def in_scope(self, path: str) -> bool:
        if not path.endswith(".py"):
            return False
        if any(path.startswith(prefix) for prefix in self.exclude):
            return False
        return not self.include or any(path.startswith(prefix) for prefix in self.include)


DEFAULT_POLICY = Policy()


def _prefixes(data: dict, key: str, label: str) -> tuple[str, ...]:
    values = data.get(key, [])
    if not isinstance(values, list):
        raise CommentsError(f"{label}: {key} must be a list")
    for prefix in values:
        if not nd._is_exact_path(prefix) or not str(prefix).endswith("/"):
            raise CommentsError(
                f"{label}: an {key} entry must be a repo-relative directory ending in /, "
                "no globs, no .."
            )
    return tuple(values)


def _id_patterns(data: dict, label: str) -> tuple[re.Pattern[str], ...]:
    if "ids" not in data:
        return DEFAULT_POLICY.ids
    values = data["ids"]
    if not isinstance(values, list):
        raise CommentsError(f"{label}: ids must be a list of regular expressions")
    out: list[re.Pattern[str]] = []
    for pattern in values:
        if not isinstance(pattern, str) or not pattern:
            raise CommentsError(f"{label}: each id pattern must be a non-empty string")
        try:
            out.append(re.compile(pattern))
        except re.error as exc:
            msg = f"{label}: id pattern {pattern!r} does not compile ({exc})"
            raise CommentsError(msg) from exc
    return tuple(out)


def parse_policy(text: str, label: str) -> Policy:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CommentsError(f"{label}: malformed policy ({exc})") from exc
    if not isinstance(data, dict):
        raise CommentsError(f"{label}: the policy file must be a JSON object")
    return Policy(
        _prefixes(data, "include", label),
        _prefixes(data, "exclude", label),
        _id_patterns(data, label),
    )


def load_policy(root: Path, rev: str | None, relpath: str = POLICY_PATH) -> Policy:
    """The policy at `rev`: the working copy (`rev is None`) or the reviewed
    commit. An absent file is the default policy; an unreadable one is a fault,
    so a broken policy cannot open the gate."""
    if rev is None:
        disk = root / relpath
        if not disk.is_file():
            return DEFAULT_POLICY
        try:
            text = disk.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise CommentsError(f"{disk}: cannot read ({type(exc).__name__})") from exc
        return parse_policy(text, str(disk))
    present, text = nd._show(root, rev, relpath)
    if not present:
        return DEFAULT_POLICY
    return parse_policy(text, f"{rev}:{relpath}")


# ------------------------------------------------------------- classification


def _is_public(name: str) -> bool:
    return not name.startswith("_")


def _doc_expr(node: ast.AST) -> ast.Expr | None:
    """The docstring statement, whose span covers any surrounding parentheses, so
    a parenthesized or concatenated docstring is one span, not string bytes with
    paren lines left counting as code."""
    body = getattr(node, "body", None)
    if not isinstance(body, list) or not body or not isinstance(body[0], ast.Expr):
        return None
    value = body[0].value
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return body[0]
    return None


def _docstrings(tree: ast.AST) -> list[tuple[ast.Expr, bool]]:
    """Every docstring statement with its exempt flag. Module, class and public
    def or method docstrings are exempt; a private helper docstring is not."""
    out: list[tuple[ast.Expr, bool]] = []
    for node in ast.walk(tree):
        expr = _doc_expr(node)
        if expr is None:
            continue
        if isinstance(node, (ast.Module, ast.ClassDef)):
            out.append((expr, True))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append((expr, _is_public(node.name)))
    return out


def _parse(source: str, label: str) -> tuple[ast.AST, list[tokenize.TokenInfo]]:
    try:
        tree = ast.parse(source)
        toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (SyntaxError, tokenize.TokenError, ValueError, RecursionError, MemoryError) as exc:
        raise CommentsError(f"{label}: cannot parse ({type(exc).__name__})") from exc
    return tree, toks


def _scan_comment(tok: tokenize.TokenInfo, line_text: str, a: Analysis) -> None:
    line, col = tok.start[0], tok.start[1]
    a.comment_text[line] = tok.string
    own = line_text[:col].strip() == ""
    if not own:
        a.trailing.add(line)
    elif _PRAGMA.match(tok.string):
        a.pragma_lines[line] = col
    else:
        a.own_comment[line] = col


def _scan_code(tok: tokenize.TokenInfo, doc_starts: set[tuple[int, int]], a: Analysis) -> None:
    if tok.type in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT):
        return
    if tok.type in (tokenize.ENCODING, tokenize.ENDMARKER):
        return
    if tok.type == tokenize.STRING and tok.start in doc_starts:
        return
    for row in range(tok.start[0], tok.end[0] + 1):
        a.code_lines.add(row)


def _analyze(source: str, label: str) -> Analysis:
    tree, toks = _parse(source, label)
    a = Analysis(source_lines=source.split("\n"))
    docs = _docstrings(tree)
    doc_starts = {(e.value.lineno, e.value.col_offset) for e, _ in docs}
    for expr, exempt in docs:
        start, end = expr.lineno, expr.end_lineno or expr.lineno
        a.doc_spans.append((start, end, exempt, expr.col_offset))
        a.doc_text[start] = expr.value.value
        for row in range(start, end + 1):
            a.doc_lines.add(row)
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            _scan_comment(tok, a.source_lines[tok.start[0] - 1], a)
        else:
            _scan_code(tok, doc_starts, a)
    return a


# ------------------------------------------------------------- blocks


def _blocks(a: Analysis) -> list[tuple[int, int, bool]]:
    """(start, end, excused) for every own-line comment block. A block is a run of
    consecutive own-line comments at one indentation; a pragma one line above at
    the same indentation excuses it."""
    lines = sorted(a.own_comment)
    out: list[tuple[int, int, bool]] = []
    i = 0
    while i < len(lines):
        start = lines[i]
        indent = a.own_comment[start]
        j = i
        while (
            j + 1 < len(lines)
            and lines[j + 1] == lines[j] + 1
            and a.own_comment[lines[j + 1]] == indent
        ):
            j += 1
        excused = a.pragma_lines.get(start - 1) == indent
        out.append((start, lines[j], excused))
        i = j + 1
    return out


def _excused_lines(blocks: list[tuple[int, int, bool]]) -> set[int]:
    out: set[int] = set()
    for start, end, excused in blocks:
        if excused:
            out.update(range(start, end + 1))
    return out


# ------------------------------------------------------------- checks


def _touches(added: set[int], start: int, end: int) -> bool:
    return any(line in added for line in range(start, end + 1))


def _check_blocks(
    path: str, blocks: list[tuple[int, int, bool]], added: set[int]
) -> list[Violation]:
    out: list[Violation] = []
    for start, end, excused in blocks:
        length = end - start + 1
        if length > MAX_BLOCK and not excused and _touches(added, start, end):
            detail = f"comment block of {length} lines (max {MAX_BLOCK})"
            out.append(Violation(path, start, detail, _FIX_BLOCK))
    return out


def _check_docstrings(path: str, a: Analysis, added: set[int]) -> list[Violation]:
    out: list[Violation] = []
    for start, end, exempt, indent in a.doc_spans:
        excused = a.pragma_lines.get(start - 1) == indent
        length = end - start + 1
        if not exempt and not excused and length > MAX_BLOCK and _touches(added, start, end):
            detail = f"docstring of {length} lines (max {MAX_BLOCK})"
            out.append(Violation(path, start, detail, _FIX_DOC))
    return out


def _first_id(text: str, ids: tuple[re.Pattern[str], ...]) -> str | None:
    for pattern in ids:
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _id_violation(path: str, line: int, found: str) -> Violation:
    detail = f"ticket or PR id '{found}' in a comment or docstring"
    return Violation(path, line, detail, _FIX_ID)


def _ids_in_comments(path: str, a: Analysis, added: set[int], policy: Policy) -> list[Violation]:
    out: list[Violation] = []
    for line, text in a.comment_text.items():
        if line in added and not _NOQA.search(text):
            found = _first_id(text, policy.ids)
            if found:
                out.append(_id_violation(path, line, found))
    return out


def _ids_in_docstrings(path: str, a: Analysis, added: set[int], policy: Policy) -> list[Violation]:
    out: list[Violation] = []
    for start, end, _exempt, _indent in a.doc_spans:
        if not _touches(added, start, end):
            continue
        found = _first_id(a.doc_text.get(start, ""), policy.ids)
        if found:
            line = min((n for n in range(start, end + 1) if n in added), default=start)
            out.append(_id_violation(path, line, found))
    return out


def _exempt_doc_lines(a: Analysis) -> set[int]:
    """Docstring lines that do not count toward the ratio numerator: an exempt
    docstring (public entry) or one a pragma one line above excuses."""
    out: set[int] = set()
    for start, end, exempt, indent in a.doc_spans:
        if exempt or a.pragma_lines.get(start - 1) == indent:
            out.update(range(start, end + 1))
    return out


def _in_numerator(line: int, a: Analysis, excused: set[int], exempt_doc: set[int]) -> bool:
    if line in a.own_comment and line not in excused:
        return True
    if line in a.trailing:
        return True
    return line in a.doc_lines and line not in exempt_doc


def _ratio_counts(
    a: Analysis, blocks: list[tuple[int, int, bool]], added: set[int]
) -> tuple[int, int]:
    excused = _excused_lines(blocks)
    exempt_doc = _exempt_doc_lines(a)
    num = sum(1 for line in added if _in_numerator(line, a, excused, exempt_doc))
    den = sum(1 for line in added if line in a.code_lines and line not in a.doc_lines)
    return num, den


def _check_ratio(
    path: str, a: Analysis, blocks: list[tuple[int, int, bool]], added: set[int]
) -> list[Violation]:
    num, den = _ratio_counts(a, blocks, added)
    if den < RATIO_FLOOR or RATIO_DEN * num <= RATIO_NUM * den:
        return []
    line = min(
        (n for n in added if n in a.own_comment or n in a.trailing or n in a.doc_lines),
        default=1,
    )
    detail = f"{num} comment lines added vs {den} code lines added (over 25%)"
    return [Violation(path, line, detail, _FIX_RATIO)]


# ------------------------------------------------------------- ERA001


def ruff_available() -> bool:
    """True when ruff is on PATH. Without it the commented-out code check is
    skipped, not failed: a host need not install ruff to run the loop."""
    return shutil.which("ruff") is not None


def _check_era001(
    root: Path, path: str, source: str, added: set[int], pragma: dict[int, int]
) -> list[Violation]:
    return [
        Violation(path, row, "commented-out code (ERA001)", _FIX_ERA)
        for row in _ruff_era001(root, path, source)
        if row in added and row not in pragma
    ]


def _ruff_era001(root: Path, path: str, source: str) -> list[int]:
    # --isolated: the host's ruff config must not switch the check off or on.
    cmd = ["ruff", "check", "--select", "ERA001", "--isolated", "--output-format", "json"]
    cmd += ["--stdin-filename", path, "-"]
    try:
        proc = subprocess.run(
            cmd, input=source, capture_output=True, text=True, cwd=root, timeout=RUFF_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise CommentsError(f"ruff timed out after {RUFF_TIMEOUT}s on {path}") from exc
    except OSError as exc:
        raise CommentsError(f"ruff could not run on {path} ({exc})") from exc
    if proc.returncode not in (0, 1):
        raise CommentsError(f"ruff exited {proc.returncode} on {path}: {proc.stderr.strip()}")
    return _parse_era001(proc.stdout, proc.returncode, path)


def _era_row(item: object, path: str) -> int | None:
    if not isinstance(item, dict) or item.get("code") != "ERA001":
        return None
    loc = item.get("location")
    row = loc.get("row") if isinstance(loc, dict) else None
    if not isinstance(row, int) or isinstance(row, bool) or row < 1:
        raise CommentsError(f"ruff ERA001 finding on {path} lacks a valid row")
    return row


def _parse_era001(stdout: str, returncode: int, path: str) -> list[int]:
    text = stdout.strip()
    if not text:
        if returncode == 1:
            raise CommentsError(f"ruff reported findings but produced no JSON on {path}")
        return []
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CommentsError(f"ruff output was not JSON on {path} ({exc})") from exc
    if not isinstance(data, list):
        raise CommentsError(f"ruff output was not a JSON list on {path}")
    rows = [row for row in (_era_row(item, path) for item in data) if row is not None]
    if returncode == 1 and not rows:
        raise CommentsError(f"ruff exit 1 but no ERA001 finding parsed on {path}")
    return rows


def check_source(
    path: str,
    source: str,
    added: set[int],
    policy: Policy = DEFAULT_POLICY,
    root: Path | None = None,
) -> list[Violation]:
    """Every violation in one file's head source on the `added` lines. The
    ERA001 check runs only when `root` is given, since ruff runs there."""
    a = _analyze(source, path)
    blocks = _blocks(a)
    out: list[Violation] = []
    out += _check_blocks(path, blocks, added)
    out += _check_docstrings(path, a, added)
    out += _ids_in_comments(path, a, added, policy)
    out += _ids_in_docstrings(path, a, added, policy)
    out += _check_ratio(path, a, blocks, added)
    if root is not None:
        out += _check_era001(root, path, source, added, a.pragma_lines)
    return out


# ------------------------------------------------------------- git scope


def _changed_python(root: Path, base_rev: str, head: str | None, policy: Policy) -> list[str]:
    out = nd._git(
        root,
        "-c",
        "core.quotepath=false",
        "diff",
        "--name-only",
        "-z",
        *nd._rev_range(base_rev, head),
    )
    return [p for p in out.split("\0") if p and policy.in_scope(p)]


def _untracked_python(root: Path, policy: Policy) -> list[str]:
    out = nd._git(
        root, "-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard", "-z"
    )
    return [p for p in out.split("\0") if p and policy.in_scope(p)]


def _source_at(root: Path, path: str, head: str | None) -> str | None:
    """The source of `path` at the diff head, or None only when the file is
    genuinely gone. A file that exists but cannot be read is a fault."""
    if head is not None:
        try:
            present, text = nd._show(root, head, path)
        except (nd.DashError, UnicodeDecodeError) as exc:
            raise CommentsError(f"{path}: cannot read at {head} ({type(exc).__name__})") from exc
        return text if present else None
    try:
        return (root / path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise CommentsError(f"{path}: cannot read ({type(exc).__name__})") from exc


def _added_set(root: Path, base_rev: str, head: str | None, path: str) -> set[int]:
    out = nd._git(
        root,
        "-c",
        "core.quotepath=false",
        "diff",
        "--text",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        *nd._rev_range(base_rev, head),
        "--",
        path,
    )
    return {line for line, _ in nd._parse_added(out)}


def scan_range(
    root: Path,
    base_ref: str,
    head: str | None,
    policy_path: str = POLICY_PATH,
    era001: bool | None = None,
) -> list[Violation]:
    """Every comment violation on a line this diff adds to an in-scope Python
    file. `era001` defaults to whether ruff is on PATH. A fault raises
    `CommentsError`, so the caller fails closed."""
    run_ruff = ruff_available() if era001 is None else era001
    ruff_root = root if run_ruff else None
    try:
        base_rev = nd.base_revision(root, base_ref, head)
        policy = load_policy(root, head, policy_path)
        out: list[Violation] = []
        for path in _changed_python(root, base_rev, head, policy):
            source = _source_at(root, path, head)
            if source is not None:
                added = _added_set(root, base_rev, head, path)
                out.extend(check_source(path, source, added, policy, ruff_root))
        if head is None:  # worktree mode: a new file is not in the diff yet
            for path in _untracked_python(root, policy):
                source = _source_at(root, path, None)
                if source is not None:
                    every = set(range(1, len(source.split("\n")) + 1))
                    out.extend(check_source(path, source, every, policy, ruff_root))
    except nd.DashError as exc:
        raise CommentsError(str(exc)) from exc
    return out


def gate(root: Path, base_ref: str, policy_path: str = POLICY_PATH) -> list[Violation]:
    """What a caller checks before it lets a stage end: every line the branch
    adds up to HEAD. A fault raises `CommentsError`, so the caller fails closed."""
    return scan_range(root, base_ref, "HEAD", policy_path)


# ------------------------------------------------------------- main


def _run(args: argparse.Namespace) -> int:
    try:
        root = nd.repo_root(Path.cwd())
    except nd.DashError as exc:
        raise CommentsError(str(exc)) from exc
    if not ruff_available():
        print(f"{RULE_ID}: ruff is not on PATH; skipping the ERA001 check.", file=sys.stderr)
    violations = scan_range(root, args.base, args.head, args.policy)
    if violations:
        for v in violations:
            print(f"  {v.message()}", file=sys.stderr)
        return 1
    print(f"{RULE_ID}: clean (the diff).")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loop-comments", description="The comment-hygiene rule over a diff."
    )
    parser.add_argument("--base", default="origin/main", help="base ref (default origin/main)")
    parser.add_argument("--head", default=None, help="explicit head commit; CI mode")
    parser.add_argument(
        "--policy",
        default=POLICY_PATH,
        help=f"repo-relative policy file (default {POLICY_PATH})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args)
    except CommentsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
