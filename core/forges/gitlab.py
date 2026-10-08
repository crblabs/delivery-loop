"""GitLab, through ``glab``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from core.forges import api_merges

# ``glab ci`` has two aliases.
_CI = ("ci", "pipe", "pipeline")


@dataclass(frozen=True)
class GitLab:
    name: str = "GitLab"
    cli: str = "glab"
    change: str = "merge request"

    def is_merge(self, args: Sequence[str]) -> bool:
        """``glab mr merge`` (or its alias ``accept``), or a merge endpoint through
        ``glab api``, such as ``projects/:id/merge_requests/:iid/merge``."""
        return list(args[:2]) in (["mr", "merge"], ["mr", "accept"]) or api_merges(args)

    def is_ci_watch(self, args: Sequence[str]) -> bool:
        """``glab ci trace``, which follows a job's log until it ends, or
        ``glab ci status --live``."""
        if not args or args[0] not in _CI:
            return False
        sub = args[1] if len(args) > 1 else ""
        return sub == "trace" or (sub == "status" and ("--live" in args or "-l" in args))


GITLAB = GitLab()
