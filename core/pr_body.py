"""Check a pull request body for the sections the host repository requires.

A reviewer reads a pull request faster when every body has the same shape. This
check keeps the shape honest: it reads a body and refuses one that omits a
required level-2 section heading.

THE SECTIONS. The host repository names them, not the plugin. `--sections`
takes a comma-separated list. Without it, the check reads `.pr-sections` at the
root of the repository the command runs in (the current directory outside a
repository): one heading per line, without the `##`, and a line whose first
character is `#` is a comment. With neither, there is nothing to check, so the
check passes and says that no sections are configured.

The check reads heading PRESENCE only. It does not enforce order or content.

A naive grep for `## Effect` is wrong, because a heading can hide where the
forge would not render it as a heading:

  * inside a fenced code block (a JSON block, a mermaid diagram),
  * inside an HTML comment (a template can fold a checklist into one),
  * inside an indented code block (four or more leading spaces).

So the body is walked line by line, in document order, tracking whether the
line is inside a fenced block or an HTML comment. Only a line the forge would
render as a level-2 heading counts: at most three leading spaces, then `##`,
then the exact section name, case-insensitive. Exact text is deliberate:
`## Effect (one sentence)` does not count, so heading drift is caught.

Input: the body on standard input, or a file path as the one argument. Exit
codes follow the shared tools contract: 0 clean, 1 refused (the missing
sections named), 2 usage or environment (empty body, unreadable or non-UTF-8
file, bad usage).

  loop-pr-body < body.md
  loop-pr-body --sections "Summary,Test plan" body.md
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from core.config import git_env

SECTIONS_PATH = ".pr-sections"

# A fence opens on <=3 leading spaces then 3+ backticks or tildes (an info
# string may follow). It closes only on a bare fence: same character, at least
# as long, then nothing but whitespace. An info string never closes a fence.
_FENCE_OPEN_RE = re.compile(r"^ {0,3}([`~]{3,})")
_FENCE_CLOSE_RE = re.compile(r"^ {0,3}([`~]{3,})[ \t]*$")
# A level-2 heading: <=3 leading spaces (four would be an indented code block),
# then `##`, a space or tab, and the name.
_HEADING_RE = re.compile(r"^ {0,3}##[ \t]+(.+?)[ \t]*$")


class BodyError(Exception):
    """A usage or environment fault. Maps to exit 2."""


def present_headings(body: str) -> set[str]:
    """The lowered text of every level-2 heading the forge would render, i.e.
    outside fenced code, indented code, and HTML comments. One pass, document
    order, so a fence inside a comment is not a fence and a comment inside a
    fence is not a comment."""
    present: set[str] = set()
    fence: str | None = None
    in_comment = False
    for raw in body.split("\n"):
        line = raw.rstrip("\r")

        if in_comment:
            end = line.find("-->")
            if end == -1:
                continue  # the whole line is still inside the comment
            line = line[end + 3 :]  # resume after the comment closes
            in_comment = False

        if fence is not None:
            close = _FENCE_CLOSE_RE.match(line)
            if close and close.group(1)[0] == fence[0] and len(close.group(1)) >= len(fence):
                fence = None
            continue  # every line inside a fence is code, never a heading

        # Outside a fence: drop inline comments, and open a multi-line one if it
        # has no closer on this line. The text before `<!--` is still live.
        while "<!--" in line:
            start = line.index("<!--")
            end = line.find("-->", start + 4)
            if end == -1:
                line = line[:start]
                in_comment = True
                break
            line = line[:start] + line[end + 3 :]

        opener = _FENCE_OPEN_RE.match(line)
        if opener:
            fence = opener.group(1)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            present.add(heading.group(1).strip().lower())
    return present


def missing_sections(body: str, sections: list[str]) -> list[str]:
    """The required sections absent from `body`, in the order given."""
    present = present_headings(body)
    return [name for name in sections if name.lower() not in present]


# ------------------------------------------------------------- sections


def parse_sections_flag(value: str) -> list[str]:
    return [name.strip() for name in value.split(",") if name.strip()]


def parse_sections_file(text: str) -> list[str]:
    """One heading per line. Blank lines and `#` comments are skipped."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


def host_root(cwd: Path) -> Path:
    """The top of the repository `cwd` is in, or `cwd` outside one. A body can be
    checked anywhere, so no repository is not a fault."""
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=cwd,
        env=git_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    top = proc.stdout.strip()
    return Path(top) if proc.returncode == 0 and top else cwd


def load_sections(flag: str | None, cwd: Path) -> list[str]:
    """The flag wins over the file. An absent file is no sections; an unreadable
    one is a fault, so a broken file cannot pass every body."""
    if flag is not None:
        return parse_sections_flag(flag)
    path = host_root(cwd) / SECTIONS_PATH
    if not path.exists():
        return []
    try:
        return parse_sections_file(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise BodyError(f"cannot read {path}: {exc}") from exc


# ------------------------------------------------------------- main


def _read_body(path: str | None) -> str:
    if path is None:
        return sys.stdin.read()
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise BodyError(f"cannot read {path}: {exc}") from exc


def _run(args: argparse.Namespace) -> int:
    body = _read_body(args.body)
    sections = load_sections(args.sections, Path.cwd())
    if not sections:
        print(f"no pull request sections are configured (--sections or {SECTIONS_PATH}).")
        return 0
    if not body.strip():
        raise BodyError("empty pull request body")
    missing = missing_sections(body, sections)
    if missing:
        print("pull request body is missing required section(s):", file=sys.stderr)
        for name in missing:
            print(f"  ## {name}", file=sys.stderr)
        print("Every pull request must carry: " + ", ".join(sections) + ".", file=sys.stderr)
        return 1
    print("pull request body has all required sections.")
    return 0


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="loop-pr-body",
        description="Check a pull request body for the host's required level-2 sections.",
    )
    parser.add_argument("body", nargs="?", default=None, help="body file (default stdin)")
    parser.add_argument(
        "--sections",
        default=None,
        help=f"comma-separated section names (default: read {SECTIONS_PATH} at the repo root)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return _run(args)
    except BodyError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
