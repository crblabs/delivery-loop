"""The no-unicode-dash rule: a ratchet on the diff.

A model reaches for the em dash and the en dash; a person types a hyphen, a
comma, a colon or a full stop. This module is the one place that decides what a
banned dash is, so every enforcement path a host wires up agrees byte for byte:
a gate over the diff, a check on a commit message or a pull-request body read
from stdin, and an audit of tracked files. Ruff RUF001 to RUF003 is a
supplementary catch for Python strings and comments; it does not flag the em
dash, so this matcher is the source of truth.

THE RANGE. Four code points, U+2012 to U+2015: figure dash, en dash, em dash,
horizontal bar. The minus sign U+2212 is a math symbol and is out of range. This
module never writes a literal dash in its own source; it builds the set with
`chr()`, so the rule does not flag the tool that enforces it.

WHAT IT GATES. Added lines only. The unit is a line the diff adds, read from
`git diff -U0`. A pre-existing dash is never flagged, so the repository is not
rewritten in bulk; a dash is fixed when a line that carries it is added. A moved
line is a deletion plus an addition, and the addition is an added line, so a
moved dashed line must comply (it is fixed, not waved through). The rule covers
every text file in the repository the command runs in, outside the directories
the policy excludes.

THE POLICY. One optional JSON file in the host repository, by default
`.dash-exceptions.json` at its root, `--exceptions` to name another. It holds
two lists, both narrow and both visible in review:
  * `exceptions`: an exact path and line with a reason. A blank reason or a glob
    is refused, so a catch-all cannot re-open the hole.
  * `exclude`: directory prefixes whose text is verbatim source, not prose (a
    data directory, captured fixtures). Each prefix ends with `/`, has no glob
    and does not climb out of the repository.
The policy is read at the diff's head (the working copy or the reviewed commit),
so a new exception takes effect in the pull request that adds the literal it
covers, visible in that diff.

An inline pragma, `no-unicode-dash: <reason>`, excepts the one line that needs a
legitimate dash (a regex range, a quoted source string). The reason is required;
a bare pragma does not except.

FAIL CLOSED. A broken policy, an unresolvable base or a failing git call is a
fault, never a silent pass.

Exit codes follow the shared tools contract: 0 clean, 1 a violation, 2 a usage
or environment fault.

  loop-no-dash                          # gate the working tree against origin/main
  loop-no-dash --head "$SHA"            # gate an explicit commit (CI)
  loop-no-dash --stdin-text --label pr  # check a commit message or a PR body
  loop-no-dash --audit docs README.md   # every tracked line under these paths
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

RULE_ID = "no-unicode-dash"
EXCEPTIONS_PATH = ".dash-exceptions.json"

# The banned code points, U+2012 to U+2015, built without a literal dash so this
# source is clean under its own rule.
DASH_CODEPOINTS = tuple(range(0x2012, 0x2016))
DASHES = frozenset(chr(cp) for cp in DASH_CODEPOINTS)

# The one-line fix, and where the reader finds the exception procedure.
_FIX = "use a hyphen, a comma, a colon or a new sentence"
_EXC = (
    f"required literal? add an exact-path reason to {EXCEPTIONS_PATH} "
    "or an inline no-unicode-dash: reason"
)

# The pragma that excepts one line, and the shortest reason it accepts.
_PRAGMA = f"{RULE_ID}:"


class DashError(Exception):
    """A usage or environment fault. Maps to exit 2."""


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    col: int
    codepoint: int

    def char_name(self) -> str:
        return unicodedata.name(chr(self.codepoint), f"U+{self.codepoint:04X}")

    def message(self) -> str:
        loc = f"{self.path}:{self.line}:{self.col}"
        cp = f"U+{self.codepoint:04X}"
        return f"{loc}: {RULE_ID}: {cp} {self.char_name()}; {_FIX}. {_EXC}"


# ------------------------------------------------------------- matcher


def dashes_in_text(text: str) -> list[tuple[int, int]]:
    """Every banned dash in one line as (1-based column, code point)."""
    return [(i + 1, ord(ch)) for i, ch in enumerate(text) if ch in DASHES]


def has_inline_pragma(text: str) -> bool:
    """True when the line carries `no-unicode-dash: <reason>` with a non-empty
    reason. A bare pragma does not except; the reason must be present so the
    exception is legible in review."""
    idx = text.find(_PRAGMA)
    if idx < 0:
        return False
    return bool(text[idx + len(_PRAGMA) :].strip())


# ------------------------------------------------------------- policy


@dataclass(frozen=True)
class ExceptionPolicy:
    """Exact (path, line) pairs a reason justifies, and the directory prefixes
    the rule skips. No globs, no whole-file exemptions: a catch-all is how a
    rule like this gets silenced."""

    allowed: frozenset[tuple[str, int]]
    excluded: tuple[str, ...] = ()

    def excepts(self, path: str, line: int) -> bool:
        return (path, line) in self.allowed

    def is_excluded(self, path: str) -> bool:
        return any(path.startswith(prefix) for prefix in self.excluded)


EMPTY_POLICY = ExceptionPolicy(frozenset())


def _is_exact_path(path: object) -> bool:
    """An exact repo-relative path: a non-empty string, no glob, not absolute, no
    `..` component. A `..` inside a filename (`version..md`) is fine."""
    return (
        isinstance(path, str)
        and bool(path)
        and "*" not in path
        and not path.startswith("/")
        and ".." not in path.split("/")
    )


def _validate_entry(entry: object, label: str) -> tuple[str, int]:
    if not isinstance(entry, dict):
        raise DashError(f"{label}: each exception must be an object")
    path, line, reason = entry.get("path"), entry.get("line"), entry.get("reason")
    if not _is_exact_path(path):
        raise DashError(f"{label}: path must be an exact repo-relative path, no globs, no ..")
    if not isinstance(line, int) or line < 1:
        raise DashError(f"{label}: line must be a positive integer (exact-line exception)")
    if not isinstance(reason, str) or not reason.strip():
        raise DashError(f"{label}: a non-empty reason is required")
    return (path, line)


def _validate_prefix(prefix: object, label: str) -> str:
    if not _is_exact_path(prefix) or not str(prefix).endswith("/"):
        raise DashError(
            f"{label}: an exclude must be a repo-relative directory ending in /, no globs, no .."
        )
    return str(prefix)


def parse_policy(text: str, label: str) -> ExceptionPolicy:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise DashError(f"{label}: malformed exceptions ({exc})") from exc
    if not isinstance(data, dict):
        raise DashError(f"{label}: exceptions file must be a JSON object")
    entries = data.get("exceptions", [])
    if not isinstance(entries, list):
        raise DashError(f"{label}: exceptions must be a list")
    prefixes = data.get("exclude", [])
    if not isinstance(prefixes, list):
        raise DashError(f"{label}: exclude must be a list")
    return ExceptionPolicy(
        frozenset(_validate_entry(e, label) for e in entries),
        tuple(_validate_prefix(p, label) for p in prefixes),
    )


def load_policy(root: Path, rev: str | None, relpath: str = EXCEPTIONS_PATH) -> ExceptionPolicy:
    """The policy at `rev`: the working copy (`rev is None`) or the reviewed
    commit (CI). It is read at the diff's head, not at the base, so a new
    exception takes effect in the same pull request that adds the literal it
    covers. That is a review-controlled escape hatch: every exception is an
    exact path, an exact line and a reason, visible in the same diff. An absent
    file is an empty policy; an unreadable object is a fault, not silently
    absent, so a broken policy cannot open the gate."""
    if rev is None:
        disk = root / relpath
        if not disk.is_file():
            return EMPTY_POLICY
        return parse_policy(disk.read_text(encoding="utf-8"), str(disk))
    present, text = _show(root, rev, relpath)
    if not present:
        return EMPTY_POLICY
    return parse_policy(text, f"{rev}:{relpath}")


# ------------------------------------------------------------- git


def _git(root: Path, *args: str) -> str:
    # Capture bytes and decode without newline translation. text=True turns an
    # embedded CR into a newline, which would split an added line and drop its
    # leading '+', letting a dash after the CR escape the parser. surrogateescape
    # keeps a non-UTF-8 byte rather than crashing; core.quotepath=false keeps a
    # non-ASCII path literal.
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    if proc.returncode != 0:
        raise DashError(
            f"git {' '.join(args)} failed: {proc.stderr.decode('utf-8', 'replace').strip()}"
        )
    return proc.stdout.decode("utf-8", "surrogateescape")


def repo_root(cwd: Path) -> Path:
    """The top of the repository `cwd` is in. Outside one is a usage fault."""
    return Path(_git(cwd, "rev-parse", "--show-toplevel").strip())


def _show(root: Path, rev: str, relpath: str) -> tuple[bool, str]:
    """(present, content) at a revision. An absent path is (False, ""); a git
    failure that is not 'path missing' raises, so an unreadable object is a
    fault rather than a false 'absent'."""
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"{rev}:{relpath}"], cwd=root, capture_output=True, text=True
    )
    if proc.returncode != 0:
        return (False, "")
    got = subprocess.run(
        ["git", "show", f"{rev}:{relpath}"], cwd=root, capture_output=True, text=True, check=False
    )
    if got.returncode != 0:
        raise DashError(f"{rev}:{relpath}: exists but is unreadable ({got.stderr.strip()})")
    return (True, got.stdout)


def base_revision(root: Path, base_ref: str, head: str | None) -> str:
    """The merge base, resolved to a SHA. A missing ref or a shallow clone that
    cannot resolve it fails loud, never a silent pass."""
    proc = subprocess.run(
        ["git", "merge-base", base_ref, head or "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise DashError(
            f"cannot resolve merge-base of {base_ref} and {head or 'HEAD'} "
            f"({proc.stderr.strip() or 'no output'}); fetch the base, do not skip the gate"
        )
    return sha


def _rev_range(base_rev: str, head: str | None) -> list[str]:
    return [base_rev, head] if head else [base_rev]


def changed_text_files(
    root: Path, base_rev: str, head: str | None, policy: ExceptionPolicy
) -> list[str]:
    """Added or modified text files outside the excluded directories. Binary
    files are dropped by numstat (git reports '-' for their line counts)."""
    out = _git(
        root, "-c", "core.quotepath=false", "diff", "--numstat", "-z", *_rev_range(base_rev, head)
    )
    return [p for p in _parse_numstat(out) if not policy.is_excluded(p)]


def _parse_numstat(z: str) -> list[str]:
    """`--numstat -z` records: added, deleted, then the path (a rename splits the
    path into two extra NUL fields after an empty inline path). A binary file has
    '-' for added and deleted; drop it."""
    fields = z.split("\0")
    out: list[str] = []
    i = 0
    while i < len(fields) and fields[i]:
        record = fields[i]
        parts = record.split("\t", 2)  # split twice: a path may itself contain a tab
        if len(parts) < 3:
            i += 1
            continue
        added, _deleted, inline = parts[0], parts[1], parts[2]
        if inline == "":  # a rename: real old and new paths follow as NUL fields
            path = fields[i + 2] if i + 2 < len(fields) else ""
            i += 3
        else:
            path = inline
            i += 1
        if added == "-":  # binary
            continue
        if path:
            out.append(path)
    return out


def added_lines(root: Path, base_rev: str, head: str | None, path: str) -> list[tuple[int, str]]:
    """(new-side line number, text) for every line this diff adds to `path`, from
    a `-U0` diff. Parsed on LF only, with hunk state, so a body line that begins
    with '+' cannot be read as the '+++' file header."""
    out = _git(
        root,
        "-c",
        "core.quotepath=false",
        "diff",
        "-U0",
        "--no-color",
        "--no-ext-diff",
        "--no-textconv",
        *_rev_range(base_rev, head),
        "--",
        path,
    )
    return _parse_added(out)


def _parse_added(diff_text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    new_line = 0
    in_hunk = False
    for raw in diff_text.split("\n"):
        if raw.startswith("@@"):
            new_line = _hunk_start(raw)
            in_hunk = True
            continue
        if not in_hunk:
            continue  # still in the file header (--- / +++ / index lines)
        if raw.startswith("\\"):
            continue  # the "\ No newline at end of file" marker, not a line
        if raw.startswith("+"):
            out.append((new_line, raw[1:]))
            new_line += 1
        elif raw.startswith("-"):
            continue  # deletion does not advance the new-side counter
        else:
            new_line += 1  # a -U0 diff has no context, but stay robust
    return out


def _hunk_start(header: str) -> int:
    """The new-side start line of a `@@ -a,b +c,d @@` header."""
    plus = header.split("+", 1)[1]
    num = plus.split(",", 1)[0].split(" ", 1)[0]
    return int(num) if num.isdigit() else 1


def untracked_text_files(root: Path, policy: ExceptionPolicy) -> list[str]:
    """Untracked, non-ignored files outside the excluded directories. In worktree
    mode a new file is not in `git diff`, so every one of its lines counts as
    added; this is how a local gate sees a dash in a file not yet committed."""
    out = _git(
        root, "-c", "core.quotepath=false", "ls-files", "--others", "--exclude-standard", "-z"
    )
    return [p for p in out.split("\0") if p and not policy.is_excluded(p)]


def whole_file_lines(root: Path, path: str) -> list[tuple[int, str]]:
    """(line number, text) for every line of a worktree file, or [] when it is
    not readable as text (a binary asset is not prose)."""
    try:
        text = (root / path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return list(enumerate(text.split("\n"), start=1))


# ------------------------------------------------------------- scans


def scan_range(
    root: Path, base_ref: str, head: str | None, exceptions: str = EXCEPTIONS_PATH
) -> list[Violation]:
    """Every banned dash on a line this diff adds, outside the excluded dirs and
    not excepted. The base and the policy share one resolved revision."""
    base_rev = base_revision(root, base_ref, head)
    policy = load_policy(root, head, exceptions)
    out: list[Violation] = []
    for path in changed_text_files(root, base_rev, head, policy):
        out.extend(_check_lines(path, added_lines(root, base_rev, head, path), policy))
    if head is None:  # worktree mode: a new file is not in the diff yet
        for path in untracked_text_files(root, policy):
            out.extend(_check_lines(path, whole_file_lines(root, path), policy))
    return out


def _check_lines(
    path: str, lines: list[tuple[int, str]], policy: ExceptionPolicy
) -> list[Violation]:
    out: list[Violation] = []
    for line, text in lines:
        if has_inline_pragma(text) or policy.excepts(path, line):
            continue
        out.extend(Violation(path, line, col, cp) for col, cp in dashes_in_text(text))
    return out


def check_text(text: str, path: str | None = None) -> list[Violation]:
    """Banned dashes in a block of new content: a commit message, a pull-request
    body, an edit an editor hook inspects. Each line honors an inline pragma.
    `path` labels the message; there is no base revision here, so the policy
    file does not apply (a new file's exceptions land with it and are caught at
    commit)."""
    out: list[Violation] = []
    for i, line in enumerate(text.split("\n"), start=1):
        if has_inline_pragma(line):
            continue
        for col, cp in dashes_in_text(line):
            out.append(Violation(path or "<input>", i, col, cp))
    return out


# ------------------------------------------------------------- audit


def audit(root: Path, paths: list[str], exceptions: str = EXCEPTIONS_PATH) -> list[Violation]:
    """Every unexcepted dash in the tracked files under `paths` (the whole
    repository when empty), so a host can show that a set of files holds zero.
    Reads the worktree and applies the on-disk policy."""
    policy = load_policy(root, None, exceptions)
    tracked = _git(root, "-c", "core.quotepath=false", "ls-files", "-z", "--", *paths)
    out: list[Violation] = []
    for path in (p for p in tracked.split("\0") if p and not policy.is_excluded(p)):
        out.extend(_check_lines(path, whole_file_lines(root, path), policy))
    return out


# ------------------------------------------------------------- main


def _report(violations: list[Violation]) -> None:
    for v in violations:
        print(f"  {v.message()}", file=sys.stderr)


def _run(args: argparse.Namespace) -> int:
    if args.stdin_text:
        label = args.label or "<stdin>"
        violations = check_text(sys.stdin.read(), label)
        where = label
    else:
        root = repo_root(Path.cwd())
        if args.audit is not None:
            violations = audit(root, args.audit, args.exceptions)
            where = "the audited files"
        else:
            violations = scan_range(root, args.base, args.head, args.exceptions)
            where = "the diff"
    if violations:
        _report(violations)
        return 1
    print(f"{RULE_ID}: clean ({where}).")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loop-no-dash", description="The no-unicode-dash rule over a diff, stdin or files."
    )
    parser.add_argument("--base", default="origin/main", help="base ref (default origin/main)")
    parser.add_argument("--head", default=None, help="explicit head commit; CI mode")
    parser.add_argument(
        "--exceptions",
        default=EXCEPTIONS_PATH,
        help=f"repo-relative policy file (default {EXCEPTIONS_PATH})",
    )
    parser.add_argument(
        "--audit",
        nargs="*",
        default=None,
        metavar="PATH",
        help="check every tracked line under these paths (all files when none given)",
    )
    parser.add_argument(
        "--stdin-text",
        action="store_true",
        help="check the text on stdin as new content (commit messages, a PR body)",
    )
    parser.add_argument("--label", default=None, help="label for --stdin-text messages")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args)
    except DashError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
