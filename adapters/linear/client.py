"""The Linear GraphQL client, and the ``Tracker`` it implements for loop-issue.

It uses the standard library only: ``urllib.request`` over the default SSL
context. When ``certifi`` happens to be importable its trust store is used
instead, for a Python whose own store is empty; it is never a dependency. The
key comes from ``LINEAR_API_KEY`` and goes in the header as
``Authorization: <key>``, with no ``Bearer`` prefix.

``LinearClient`` takes a ``transport``, the one function that sends a request
body and returns the response body, so a test fakes the network there and never
reaches the API. Every bad answer raises ``TrackerError``: a failed request,
malformed JSON, an ``errors`` block, no ``data``, or a page with no cursor.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime

from core.issue_draft import (
    AuditIssue,
    AuditProject,
    IssueInput,
    TrackerError,
    Written,
)

API_URL = "https://api.linear.app/graphql"
ENV_KEY = "LINEAR_API_KEY"
TIMEOUT_S = 45
# A bounded page loop; a workspace is far smaller than this ceiling.
MAX_PAGES = 200

# Which issue and project states count as active work.
ACTIVE_ISSUE_STATES = frozenset({"triage", "backlog", "unstarted", "started"})
INACTIVE_PROJECT_STATES = frozenset({"completed", "canceled"})

Transport = Callable[[bytes, dict[str, str]], bytes]

_PAGE = "pageInfo{hasNextPage endCursor}"
_TEAMS_Q = f"query($after:String){{teams(first:100,after:$after){{nodes{{id name key}}{_PAGE}}}}}"
_PROJECTS_Q = f"query($after:String){{projects(first:100,after:$after){{nodes{{id name}}{_PAGE}}}}}"
_LABELS_Q = (
    f"query($after:String){{issueLabels(first:100,after:$after){{nodes{{id name}}{_PAGE}}}}}"
)
_MILESTONES_Q = "query($id:String!){project(id:$id){projectMilestones(first:100){nodes{id name}}}}"
_ISSUE_Q = "query($id:String!){issue(id:$id){id identifier}}"
_CREATE_M = (
    "mutation($input:IssueCreateInput!){issueCreate(input:$input)"
    "{success issue{id identifier url}}}"
)
_UPDATE_M = (
    "mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input)"
    "{success issue{id identifier url}}}"
)
_AUDIT_ISSUES_Q = (
    "query($after:String){issues(first:100,after:$after){nodes{id description "
    f"projectMilestone{{id}} project{{id}} state{{type}}}}{_PAGE}}}}}"
)
_AUDIT_PROJECTS_Q = (
    "query($after:String){projects(first:100,after:$after){nodes{id name state "
    f"projectUpdates(first:1){{nodes{{createdAt}}}}}}{_PAGE}}}}}"
)


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def urllib_transport(body: bytes, headers: dict[str, str]) -> bytes:
    request = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S, context=_ssl_context()) as resp:
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TrackerError(f"Linear API request failed: {exc}") from exc


class LinearClient:
    """A thin GraphQL client. A bad answer raises ``TrackerError``."""

    def __init__(self, key: str, transport: Transport = urllib_transport) -> None:
        self._key = key
        self._transport = transport

    def graphql(self, query: str, variables: dict | None = None) -> dict:
        payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
        headers = {"Authorization": self._key, "Content-Type": "application/json"}
        raw = self._transport(payload, headers)
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError as exc:
            raise TrackerError("Linear API returned malformed JSON") from exc
        if not isinstance(parsed, dict):
            raise TrackerError("Linear API returned a response that is not an object")
        if parsed.get("errors"):
            raise TrackerError(f"Linear API returned errors: {parsed['errors']}")
        if not isinstance(parsed.get("data"), dict):
            raise TrackerError("Linear API returned no data")
        return parsed["data"]

    def paginate(self, query: str, connection: str) -> Iterator[dict]:
        """Every node of a top-level connection, following the cursor."""
        cursor = None
        for _ in range(MAX_PAGES):
            page = self.graphql(query, {"after": cursor}).get(connection)
            if not isinstance(page, dict) or "nodes" not in page or "pageInfo" not in page:
                raise TrackerError(f"Linear API returned an incomplete `{connection}` page")
            yield from page["nodes"]
            if not page["pageInfo"].get("hasNextPage"):
                return
            cursor = page["pageInfo"].get("endCursor")
            if not cursor:
                raise TrackerError(f"Linear API paged `{connection}` without a cursor")
        raise TrackerError(f"Linear API paging of `{connection}` exceeded {MAX_PAGES} pages")


def _find_named(nodes, name: str) -> str | None:
    for node in nodes:
        if name in (node.get("name"), node.get("key")):
            return node.get("id")
    return None


def _input(issue: IssueInput) -> dict:
    """The mutation input. A name the draft left out is left out here too, so
    an update never blanks a field."""
    payload: dict = {"title": issue.title, "description": issue.body, "priority": issue.priority}
    optional = {
        "teamId": issue.team_id,
        "projectId": issue.project_id,
        "projectMilestoneId": issue.milestone_id,
        "labelIds": list(issue.label_ids) if issue.label_ids is not None else None,
    }
    payload.update({k: v for k, v in optional.items() if v is not None})
    return payload


def _written(data: dict, field: str) -> Written:
    result = data.get(field) or {}
    issue = result.get("issue")
    if not result.get("success") or not isinstance(issue, dict):
        # Never retried: a create that half-succeeded would be filed twice.
        raise TrackerError(f"{field} did not report success; not retrying.")
    return Written(identifier=str(issue.get("identifier")), url=str(issue.get("url")))


def _last_update(node: dict) -> datetime | None:
    updates = (node.get("projectUpdates") or {}).get("nodes") or []
    if not updates:
        return None
    try:
        return datetime.fromisoformat(str(updates[0]["createdAt"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise TrackerError(f"Linear API returned a bad project update date: {exc}") from exc


class LinearTracker:
    """``core.issue_draft.Tracker`` over one ``LinearClient``."""

    name = "linear"

    def __init__(self, client: LinearClient) -> None:
        self.client = client

    def resolve_team(self, name: str) -> str | None:
        return _find_named(self.client.paginate(_TEAMS_Q, "teams"), name)

    def resolve_project(self, name: str) -> str | None:
        return _find_named(self.client.paginate(_PROJECTS_Q, "projects"), name)

    def resolve_milestone(self, project_id: str, name: str) -> str | None:
        data = self.client.graphql(_MILESTONES_Q, {"id": project_id})
        try:
            nodes = data["project"]["projectMilestones"]["nodes"]
        except (KeyError, TypeError) as exc:
            raise TrackerError("Linear API returned no milestones for the project") from exc
        return _find_named(nodes, name)

    def resolve_labels(self, names: list[str]) -> dict[str, str | None]:
        known = list(self.client.paginate(_LABELS_Q, "issueLabels"))
        return {name: _find_named(known, name) for name in names}

    def create_issue(self, issue: IssueInput) -> Written:
        return _written(self.client.graphql(_CREATE_M, {"input": _input(issue)}), "issueCreate")

    def update_issue(self, reference: str, issue: IssueInput) -> Written:
        found = self.client.graphql(_ISSUE_Q, {"id": reference}).get("issue")
        if not isinstance(found, dict) or not found.get("id"):
            raise TrackerError(f"update target `{reference}` does not resolve to an issue.")
        variables = {"id": found["id"], "input": _input(issue)}
        return _written(self.client.graphql(_UPDATE_M, variables), "issueUpdate")

    def active_issues(self) -> Iterator[AuditIssue]:
        for node in self.client.paginate(_AUDIT_ISSUES_Q, "issues"):
            if (node.get("state") or {}).get("type") not in ACTIVE_ISSUE_STATES:
                continue
            yield AuditIssue(
                has_milestone=bool(node.get("projectMilestone")),
                has_project=bool(node.get("project")),
                body=node.get("description") or "",
            )

    def active_projects(self) -> Iterator[AuditProject]:
        for node in self.client.paginate(_AUDIT_PROJECTS_Q, "projects"):
            if node.get("state") in INACTIVE_PROJECT_STATES:
                continue
            yield AuditProject(name=str(node.get("name")), last_update=_last_update(node))
