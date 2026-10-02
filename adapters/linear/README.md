# adapters/linear

The adapter for one issue tracker: Linear, through its GraphQL API.

## What belongs here

- The GraphQL queries and mutations `loop-issue` needs, and the client that
  sends them.
- The mapping from `core.issue_draft`'s `Tracker` protocol to Linear: a name to
  an id, an `IssueInput` to a mutation input, a Linear issue or project to an
  `AuditIssue` or `AuditProject`.
- What counts as active in Linear: an issue whose state type is triage,
  backlog, unstarted or started, and a project that is not completed or
  canceled.
- The key: `LINEAR_API_KEY` from the environment, sent as `Authorization:
  <key>` with no `Bearer` prefix.

## What does not belong here

- The draft format, the lint rules, the audit rules and the ratchet. Those are
  `core/issue_draft.py`.
- The command line. That is `core/issue_writer.py`.
- The guard that denies Linear's own MCP write tools during a run. A tool name
  is the harness's, so that list is in `adapters/claude_code/bootstrap.py`.

## What is here

| Module | Does |
|---|---|
| `__init__.py` | `connect(environ, required)`: the tracker for the key in the environment, `None` without one, or a `TrackerError` (exit 2) when the key is required. |
| `client.py` | `LinearClient`, a GraphQL client over `urllib.request`, and `LinearTracker`, the `Tracker` it implements. |

The client uses the standard library only. It verifies TLS with the default SSL
context, or with `certifi`'s trust store when `certifi` happens to be
importable; it is never a dependency. A test passes a fake `transport` to
`LinearClient`, so no test reaches the API.
