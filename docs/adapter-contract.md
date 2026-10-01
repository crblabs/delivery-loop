# The adapter contract

This file says what a harness must be able to do before the delivery loop can
run on it, and what the seam between an adapter and `core/` looks like.

The signatures below are a sketch, not shipped code. They exist to fix the
shape of the seam while the scope is still being settled. Where a detail is not
decided, the text says "unsettled".

## The four capabilities

| # | Capability | Required | If absent |
|---|---|---|---|
| 1 | A turn-end event the adapter can block and reinject into | Yes | The loop cannot run at all. |
| 2 | A pre-tool check that can deny a command | Yes | Guards cannot be enforced. |
| 3 | A session identity | Yes | A run cannot be tied to a session. |
| 4 | A read of the session's pending question | No | The loop degrades to state-file-only. |

**Capability 1 is absolute.** A harness that does not fire a turn-end event, or
fires one it will not let the adapter block, cannot run the loop. There is no
fallback.

## Shared types

```python
from pathlib import Path
from typing import Literal, Protocol, TypedDict

SessionId = str
RunId = str

Stage = Literal["autoplan", "implement", "qa", "review", "ship"]
```

## Capability 1: the blocking turn-end event

The harness fires an event when the agent finishes a turn. The adapter parses
it, hands core a `TurnEnd`, and returns core's `TurnEndVerdict` to the harness.

```python
class TurnEnd(TypedDict):
    session_id: SessionId
    # The message the turn ended on. The adapter reads it from the harness,
    # from the event itself or the turn record, so core never parses a
    # transcript.
    message: str | None

class TurnEndVerdict(TypedDict):
    # False lets the turn end. True holds the session open.
    block: bool
    # Text injected back into the session when block is True. None when the
    # loop is blocking without saying anything, which is unsettled.
    reinject: str | None
    # Set when the loop is pausing rather than advancing. Names the pause
    # reason from the taxonomy in core.
    pause_reason: str | None
```

What core exposes:

```python
def handle_turn_end(event: TurnEnd, state_path: Path) -> TurnEndVerdict: ...
```

A block cannot loop without end: core bounds the blocks in one stage by the
stage's attempts, and pauses the run when they run out.

What the adapter must do with the verdict:

- `block` False: let the turn end, unchanged.
- `block` True with `reinject` set: hold the turn open and put that text into
  the session as the next thing the agent reads.
- `pause_reason` set: the run is paused. The adapter does not act on this
  beyond returning the verdict. The supervisor reads the pause from the state
  file.

```python
def emit_turn_end(self, verdict: TurnEndVerdict) -> int: ...
```

The return value is whatever the harness reads as a result, commonly a process
exit code. Which codes mean what is harness-specific and belongs in the
adapter.

## Capability 2: the pre-tool check

The harness calls the adapter before a tool runs. A denial must stop the tool.

```python
class PreToolCall(TypedDict):
    session_id: SessionId
    tool_name: str
    # The shell command, when the tool is a shell tool. None otherwise.
    command: str | None
    arguments: dict[str, object]

class PreToolVerdict(TypedDict):
    allow: bool
    # Shown to the agent when allow is False, so it can change course.
    reason: str | None
    # The guard hash of the command core evaluated. An approval is recorded
    # against this hash, so it applies to one command and not to a family.
    guard_hash: str | None
```

What core exposes:

```python
def handle_pre_tool(call: PreToolCall, state_path: Path) -> PreToolVerdict: ...
```

Core decides using loop-path classification and the carve-out list. The adapter
supplies no policy of its own.

```python
def emit_pre_tool(self, verdict: PreToolVerdict) -> int: ...
```

## Capability 3: session identity

The identifier must be stable for the life of a session and must be the same
value on both the turn-end event and the pre-tool check. Core uses it to find
the run that a session belongs to.

```python
def session_id(self, raw: dict[str, object]) -> SessionId: ...
```

Whether a session identifier is expected to survive a harness restart is
unsettled.

## Capability 4: the pending question, optional

When the harness can report what the agent is currently asking the operator,
the supervisor can escalate the question itself rather than only the fact of a
pause.

```python
def pending_question(self, session: SessionId) -> str | None: ...
```

- Returns the question text when the harness exposes it.
- Returns `None` when there is no pending question.
- An adapter that cannot support this at all declares
  `supports_pending_question = False` and the loop runs state-file-only: the
  supervisor escalates the pause reason and the decision card from the state
  file, and does not quote a question.

## The adapter protocol

```python
class Adapter(Protocol):
    name: str
    supports_pending_question: bool

    def parse_turn_end(self, raw: dict[str, object]) -> TurnEnd: ...
    def emit_turn_end(self, verdict: TurnEndVerdict) -> int: ...

    def parse_pre_tool(self, raw: dict[str, object]) -> PreToolCall: ...
    def emit_pre_tool(self, verdict: PreToolVerdict) -> int: ...

    def session_id(self, raw: dict[str, object]) -> SessionId: ...
    def pending_question(self, session: SessionId) -> str | None: ...
```

## What core passes and expects back

| Direction | Core gives | Core expects |
|---|---|---|
| Turn end | Nothing. The adapter calls core. | A parsed `TurnEnd`, with a real session identity. |
| Turn end | A `TurnEndVerdict`. | The turn blocked and the text reinjected, when asked. |
| Pre tool | Nothing. The adapter calls core. | A parsed `PreToolCall`. |
| Pre tool | A `PreToolVerdict`. | The tool denied when `allow` is False. |
| Pending question | A session identity. | The question text, or `None`. |

Core reads and writes the state file. An adapter never does.

The state file lives outside the repository, under the state root
(`~/.delivery-loop` unless `DELIVERY_LOOP_HOME`, or `state_root` in a file
named with `--config`, moves it), at
the path `core.pipeline_state.state_path(worktree, repo)` returns. Before the
first write, core calls `prepare_run_dir`, which records the worktree the run
belongs to. The edit guard classifies repository paths only, so it does not
see the state file. A guard that accepts absolute paths must also refuse every
path under the state root.

## Claude Code

`adapters/claude_code/` is the one adapter. It ships as a Claude Code plugin
whose entry points sit at the repository root.

| # | Capability | How Claude Code provides it |
|---|---|---|
| 1 | Blocking turn end | The `Stop` hook (`hooks/pipeline_stop.py`). A block prints `{"decision": "block", "reason": <reinject>}`; `stop_hook_active` in the payload marks a turn end that follows a block. |
| 2 | Pre-tool denial | The `PreToolUse` hook on `Edit`, `Write`, `MultiEdit`, `NotebookEdit`, `Bash`, `Skill` and MCP tools (`hooks/pipeline_guard.py`). A denial prints a `hookSpecificOutput` with `permissionDecision: "deny"`. |
| 3 | Session identity | `session_id` in every payload; the slash command passes `${CLAUDE_SESSION_ID}` to `delivery-loop start`. |
| - | A person's own action | The `UserPromptSubmit` hook (`hooks/pipeline_prompt.py`) fires only for what a person types, so a typed resume or abort is applied there, with the session the harness names. |
| 4 | Pending question | `supervisor_transcript.py` reads the session transcript. Supported. |

In the code, core's call types are `core.turn_end.TurnEnd` and
`core.guard.PreToolCall`, and a run is found through the run index
(`core.run_index`) rather than passed a state path.

## Settled

- **The error path.** A payload that does not parse fails open: with no
  readable `cwd` there is no run to protect. A run whose state cannot be read
  fails closed: the guard denies, and the Stop hook blocks once per turn, never
  when the turn end follows a block of ours, so it cannot loop. An index entry
  whose state file is gone is stale: it is removed and the call passes.
- **`reinject` while blocking.** Every block carries text: the stage to do, or
  why the stage is not finished.
- **The guard hash.** Core keeps it. The adapter never sees the guard map.

## Unsettled

- Whether a session identity must survive a harness restart. A run bound to a
  session that ends is driven again only after a person resumes it from a new
  session, which binds that session.
