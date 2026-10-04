"""The Linear adapter, over a fake transport: no test reaches the API."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from adapters import linear
from adapters.linear import client as lc
from core.issue_draft import IssueInput, TrackerError, run_audit


def _page(nodes: list, more: bool = False, cursor: str | None = None) -> dict:
    return {"nodes": nodes, "pageInfo": {"hasNextPage": more, "endCursor": cursor}}


def _client(handler: Callable[[dict], object], calls: list | None = None) -> lc.LinearClient:
    def transport(body: bytes, headers: dict[str, str]) -> bytes:
        payload = json.loads(body)
        if calls is not None:
            calls.append((payload, headers))
        answer = handler(payload)
        return answer if isinstance(answer, bytes) else json.dumps(answer).encode()

    return lc.LinearClient("lin_key", transport)


def _resolve_handler(payload: dict) -> dict:
    query = payload["query"]
    if "teams(" in query:
        return {"data": {"teams": _page([{"id": "T", "name": "Platform", "key": "ENG"}])}}
    if "projectMilestones" in query:
        nodes = [{"id": "M", "name": "Hardening"}]
        return {"data": {"project": {"projectMilestones": {"nodes": nodes}}}}
    if "projects(" in query:
        return {"data": {"projects": _page([{"id": "P", "name": "Delivery"}])}}
    if "issueLabels(" in query:
        return {"data": {"issueLabels": _page([{"id": "L", "name": "tooling"}])}}
    raise AssertionError(f"unexpected query: {query}")


def test_connect_needs_the_key_only_when_required() -> None:
    assert linear.connect({}, required=False) is None
    with pytest.raises(TrackerError, match="LINEAR_API_KEY"):
        linear.connect({"LINEAR_API_KEY": "  "}, required=True)
    assert isinstance(linear.connect({"LINEAR_API_KEY": "k"}, required=True), lc.LinearTracker)


def test_the_key_is_sent_without_a_bearer_prefix() -> None:
    calls: list = []
    _client(lambda p: {"data": {}}, calls).graphql("query{x}")
    assert calls[0][1]["Authorization"] == "lin_key"


def test_names_resolve_by_name_or_team_key() -> None:
    tracker = lc.LinearTracker(_client(_resolve_handler))
    assert tracker.resolve_team("Platform") == "T"
    assert tracker.resolve_team("ENG") == "T"
    assert tracker.resolve_team("Nope") is None
    assert tracker.resolve_project("Delivery") == "P"
    assert tracker.resolve_milestone("P", "Hardening") == "M"
    assert tracker.resolve_labels(["tooling", "x"]) == {"tooling": "L", "x": None}


@pytest.mark.parametrize(
    "answer",
    [{"errors": [{"message": "nope"}]}, {"data": None}, b"not json", b"[]"],
    ids=["errors", "no-data", "malformed", "not-an-object"],
)
def test_a_bad_answer_is_a_tracker_error(answer: object) -> None:
    with pytest.raises(TrackerError):
        _client(lambda p: answer).graphql("query{x}")


def test_a_page_with_more_but_no_cursor_is_a_tracker_error() -> None:
    client = _client(lambda p: {"data": {"teams": _page([], more=True)}})
    with pytest.raises(TrackerError):
        list(client.paginate("query{teams}", "teams"))


def test_create_sends_only_the_fields_the_draft_named() -> None:
    calls: list = []
    issue = {"id": "I", "identifier": "ENG-9", "url": "u"}
    client = _client(lambda p: {"data": {"issueCreate": {"success": True, "issue": issue}}}, calls)
    written = lc.LinearTracker(client).create_issue(IssueInput("t", "b", 3, team_id="T"))
    assert written.identifier == "ENG-9"
    sent = calls[0][0]["variables"]["input"]
    assert sent == {"title": "t", "description": "b", "priority": 3, "teamId": "T"}


def test_a_create_that_does_not_report_success_is_not_retried() -> None:
    calls: list = []
    client = _client(lambda p: {"data": {"issueCreate": {"success": False, "issue": None}}}, calls)
    with pytest.raises(TrackerError):
        lc.LinearTracker(client).create_issue(IssueInput("t", "b", 3))
    assert len(calls) == 1


def test_update_resolves_the_reference_first() -> None:
    def handler(payload: dict) -> dict:
        if "issueUpdate" in payload["query"]:
            assert payload["variables"]["id"] == "uuid-4"
            issue = {"id": "uuid-4", "identifier": "ENG-4", "url": "u"}
            return {"data": {"issueUpdate": {"success": True, "issue": issue}}}
        return {"data": {"issue": {"id": "uuid-4", "identifier": "ENG-4"}}}

    written = lc.LinearTracker(_client(handler)).update_issue("ENG-4", IssueInput("t", "b", 1))
    assert written.identifier == "ENG-4"
    missing = lc.LinearTracker(_client(lambda p: {"data": {"issue": None}}))
    with pytest.raises(TrackerError):
        missing.update_issue("ENG-5", IssueInput("t", "b", 1))


def test_the_audit_counts_active_work_across_pages() -> None:
    def issue(state: str, milestone=None, project=None, body: str = "x") -> dict:
        return {
            "state": {"type": state},
            "projectMilestone": milestone,
            "project": project,
            "description": body,
        }

    issues = {
        None: _page([issue("backlog"), issue("completed")], more=True, cursor="c1"),
        "c1": _page([issue("started", {"id": "M"}, {"id": "P"}, "word " * 200)]),
    }
    projects = [
        {"name": "a", "state": "started", "projectUpdates": {"nodes": []}},
        {"name": "b", "state": "completed", "projectUpdates": {"nodes": []}},
        {
            "name": "c",
            "state": "started",
            "projectUpdates": {"nodes": [{"createdAt": "2026-09-20T00:00:00Z"}]},
        },
    ]

    def handler(payload: dict) -> dict:
        if "issues(" in payload["query"]:
            return {"data": {"issues": issues[payload["variables"]["after"]]}}
        return {"data": {"projects": _page(projects)}}

    now = datetime(2026, 9, 21, tzinfo=UTC)
    counts = run_audit(lc.LinearTracker(_client(handler)), now)
    assert counts == {"P1": 1, "P2": 1, "P3": 1, "P4": 1}
