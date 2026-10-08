"""The code hosts a run reaches through a CLI: one interface, one implementation each.

The guard reads shell lines for two things a code host's CLI can do: merge a
pull or merge request, which is always a person's call, and watch CI until it
finishes, which is a foreground wait. Each host answers both from the words
after its CLI's name; the guard never names a CLI itself.

Adding a host is one module here with a ``Forge`` and one entry in ``FORGES``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

# The REST endpoints that merge: a pull request's /merge, a repository's /merges,
# a GitLab merge request's /merge. ``/merge_requests`` itself does not match.
MERGE_PATH_RE = re.compile(r"/merges?(?:$|[/?])")


class Forge(Protocol):
    name: str
    # The CLI's program name, as it appears first on a shell command.
    cli: str
    # What the host calls the change a run opens, for the guard's messages.
    change: str

    def is_merge(self, args: Sequence[str]) -> bool:
        """Whether the CLI call, by its arguments after the program, merges."""
        ...

    def is_ci_watch(self, args: Sequence[str]) -> bool:
        """Whether the CLI call waits in the foreground until CI finishes."""
        ...


def api_merges(args: Sequence[str]) -> bool:
    """A call through the CLI's ``api`` subcommand to an endpoint that merges."""
    return args[:1] == ["api"] and any(MERGE_PATH_RE.search(a) for a in args[1:])


def _all() -> tuple[Forge, ...]:
    from core.forges.github import GITHUB
    from core.forges.gitlab import GITLAB

    return (GITHUB, GITLAB)


FORGES: tuple[Forge, ...] = _all()
CLIS: tuple[str, ...] = tuple(f.cli for f in FORGES)


def by_cli(program: str) -> Forge | None:
    """The host whose CLI is ``program``, else ``None``."""
    return next((f for f in FORGES if f.cli == program), None)
