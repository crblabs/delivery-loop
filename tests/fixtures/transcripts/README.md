# Transcript fixtures

Lines in Claude Code's session transcript shape, one record kind per file, for
`adapters/claude_code/transcript_tasks.py`. They copy the shapes of real local
transcripts (Claude Code 2.1.289, 2026-10-04): an async agent launch, a
background Bash launch, a Bash call moved to the background at its timeout, a
`queued_command` delivery, an idle `user` delivery, a multi-id `stopped`
delivery, a queued but undelivered notification, a `TaskStop` result, an id-less
stop, a sidechain entry, tool output that only prints a `<task-id>`, and
garbage lines. Paths and prompts are trimmed.

Timestamps are placeholders, `{T+<seconds>}`, filled by the `transcript`
fixture in `tests/conftest.py` relative to the run a test starts, so the
scanner's `since` filter sees them as the run's own records.

`killshell_stop.jsonl` is the one synthetic shape: the `KillShell` result of
older Claude Code versions (`shell_id` and "Successfully killed shell"), taken
from their tool's documented output. No local transcript holds one, so it is
unconfirmed; a wrong guess only means a stop is not seen.

`monitor_launch`, `monitor_event`, `monitor_ended`, `agent_still_running` and
`agent_resumed` copy records from probes run in a Claude Code 2.1.289 session
on 2026-10-04 (a Monitor, a subagent that started background work of its own,
and a finished agent woken by `SendMessage`). `bash_watch_launch` pairs a
background `gh run watch` tool call with its launch result.
