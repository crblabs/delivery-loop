"""The pre-push gate: the rules the Stop hook enforces, run on what a push sends.

The Stop hook binds an agent's run. A person's own push never passes through
it, so this command gives a host the same rules at `git push`. Git runs a
pre-push hook with the remote's name and url as arguments and one line per
pushed ref on stdin: `<local ref> <local sha> <remote ref> <remote sha>`. A host
installs it as `.git/hooks/pre-push`:

    #!/bin/sh
    exec loop-pre-push "$@"

For every pushed ref it runs:
  * the no-unicode-dash rule on the lines the branch adds, and on the message
    of every commit the push sends;
  * the comment rule, when `.comments-policy.json` is in the pushed commit;
  * the complexity ratchet, when `.complexity-baseline.json` is in it.

THE BASE. A branch is measured from where it left `origin/HEAD`, never from the
remote's old tip: after a rebase, `remote..local` holds the base branch's own
commits, and the gate would blame the branch for them. With no `origin/HEAD`,
the base is the branch's root commit. Commit messages are the exception: they
are read from the remote's old tip when that tip is an ancestor, so a message
that is already on the remote is not refused again.

Exit codes: 0 clean, 1 a rule refused, 2 a fault. Git refuses the push on any
non-zero code. `git push --no-verify` skips the hook.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from core import comments_gate as cg
from core import complexity_gate as cx
from core import no_unicode_dash as nd
from core.config import git_env

ZERO = "0" * 40


@dataclass(frozen=True)
class Push:
    """One pushed ref, as git writes it on the hook's stdin."""

    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str


def parse_pushes(text: str) -> list[Push]:
    """The refs a push sends. A deleted branch sends nothing to check."""
    out: list[Push] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 4:
            continue
        push = Push(*parts)
        if push.local_sha.strip("0"):
            out.append(push)
    return out


def _git_ok(root: Path, *args: str) -> str | None:
    """Stripped output, or None when git exits non-zero."""
    done = subprocess.run(
        ["git", *args], cwd=root, env=git_env(), capture_output=True, text=True, check=False
    )
    out = done.stdout.strip()
    return out if done.returncode == 0 and out else None


def base_ref(root: Path, sha: str) -> str:
    """Where the branch left `origin/HEAD`, or its root commit without one."""
    found = _git_ok(root, "merge-base", "origin/HEAD", sha)
    if found:
        return found
    roots = _git_ok(root, "rev-list", "--max-parents=0", sha)
    if not roots:
        raise nd.DashError(f"cannot find a base for {sha}")
    return roots.splitlines()[-1]


def _message_base(root: Path, push: Push, base: str) -> str:
    """The remote's old tip when the push extends it, else the branch's base."""
    if not push.remote_sha.strip("0"):
        return base
    done = subprocess.run(
        ["git", "merge-base", "--is-ancestor", push.remote_sha, push.local_sha],
        cwd=root,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    return push.remote_sha if done.returncode == 0 else base


def _has_file(root: Path, sha: str, relpath: str) -> bool:
    done = subprocess.run(
        ["git", "cat-file", "-e", f"{sha}:{relpath}"],
        cwd=root,
        env=git_env(),
        capture_output=True,
        check=False,
    )
    return done.returncode == 0


_Check = Callable[[Path, str, str], list]


def _dash_findings(root: Path, push: Push, base: str) -> list:
    found = list(nd.scan_range(root, base, push.local_sha))
    return found + nd.commit_messages(root, _message_base(root, push, base), push.local_sha)


# The rules a host opts in to: its policy file, its label, and the check.
_OPT_IN: tuple[tuple[str, str, _Check], ...] = (
    (cg.POLICY_PATH, cg.RULE_ID, lambda root, base, sha: cg.scan_range(root, base, sha)),
    (cx.BASELINE_PATH, "complexity", lambda root, base, sha: cx.check(root, base, sha)),
)


def check_push(root: Path, push: Push) -> list[str]:
    """Every refusal for one pushed ref, as the lines to print."""
    base = base_ref(root, push.local_sha)
    lines = [f"  {v.message()}" for v in _dash_findings(root, push, base)]
    for policy, _label, check in _OPT_IN:
        if _has_file(root, push.local_sha, policy):
            lines.extend(f"  {f.message()}" for f in check(root, base, push.local_sha))
    return lines


_FAULTS = (nd.DashError, cg.CommentsError, cx.GateError)


def main(argv: list[str] | None = None) -> int:
    # Git passes the remote's name and url; the refs on stdin are all it needs.
    del argv
    try:
        root = nd.repo_root(Path.cwd())
        refused: list[str] = []
        for push in parse_pushes(sys.stdin.read()):
            lines = check_push(root, push)
            if lines:
                refused.append(f"pre-push: {push.local_ref} is refused:")
                refused.extend(lines)
    except _FAULTS as exc:
        print(f"pre-push: error: {exc}", file=sys.stderr)
        return 2
    if refused:
        print("\n".join(refused), file=sys.stderr)
        print("pre-push: fix the above, or bypass with git push --no-verify.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
