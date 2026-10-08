"""The code-host interface: every host answers the same two questions."""

from __future__ import annotations

import pytest

from core import guard
from core.forges import CLIS, FORGES, by_cli


def test_github_and_gitlab_are_the_hosts() -> None:
    assert CLIS == ("gh", "glab")
    assert {f.name for f in FORGES} == {"GitHub", "GitLab"}
    assert by_cli("glab") is not None and by_cli("git") is None


@pytest.mark.parametrize(
    ("cli", "args", "merges"),
    [
        ("gh", ["pr", "merge", "7"], True),
        ("gh", ["api", "repos/o/r/pulls/7/merge"], True),
        ("gh", ["pr", "view", "7"], False),
        ("glab", ["mr", "merge", "7"], True),
        ("glab", ["mr", "accept", "7"], True),
        ("glab", ["api", "projects/1/merge_requests/7/merge"], True),
        ("glab", ["api", "projects/1/merge_requests/7"], False),
        ("glab", ["mr", "create"], False),
    ],
)
def test_each_host_names_its_merges(cli: str, args: list[str], merges: bool) -> None:
    forge = by_cli(cli)
    assert forge is not None and forge.is_merge(args) is merges


@pytest.mark.parametrize(
    ("command", "watch"),
    [
        ("gh run watch 1", True),
        ("glab ci trace", True),
        ("glab ci status --live", True),
        ("glab ci status", False),
        ("sleep 5", False),
    ],
)
def test_a_ci_watch_is_known_for_each_host(command: str, watch: bool) -> None:
    # Value: protects=a background CI watch on GitLab is judged like gh's; fails_when=
    # ci_watch names one CLI; why_new=core.forges; seam=none
    assert guard.ci_watch(command) is watch
