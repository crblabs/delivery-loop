"""GitHub, through ``gh``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.forges import api_merges


def _watching(args: Sequence[str]) -> bool:
    return any(a == "--watch" or (a.startswith("--watch=") and a != "--watch=false") for a in args)


@dataclass(frozen=True)
class GitHub:
    name: str = "GitHub"
    cli: str = "gh"
    change: str = "pull request"

    def is_merge(self, args: Sequence[str]) -> bool:
        """``gh pr merge``, or a merge endpoint through ``gh api``."""
        return list(args[:2]) == ["pr", "merge"] or api_merges(args)

    def is_ci_watch(self, args: Sequence[str]) -> bool:
        """``gh run watch``, or ``gh pr checks`` / ``gh run view`` with ``--watch``."""
        head = list(args[:2])
        return head == ["run", "watch"] or (
            head in (["pr", "checks"], ["run", "view"]) and _watching(args)
        )


GITHUB = GitHub()
