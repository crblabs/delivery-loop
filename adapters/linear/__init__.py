"""The adapter for one tracker: Linear, through its GraphQL API."""

from __future__ import annotations

from collections.abc import Mapping

from adapters.linear.client import ENV_KEY, LinearClient, LinearTracker
from core.issue_draft import TrackerError

__all__ = ["ENV_KEY", "LinearClient", "LinearTracker", "connect"]


def connect(environ: Mapping[str, str], required: bool) -> LinearTracker | None:
    """The tracker for the key in ``environ``. With no key, ``None``, or a
    ``TrackerError`` (exit 2) when the caller cannot go on without one."""
    key = environ.get(ENV_KEY, "").strip()
    if key:
        return LinearTracker(LinearClient(key))
    if required:
        raise TrackerError(f"{ENV_KEY} is not set; a write and the audit need it.")
    return None
