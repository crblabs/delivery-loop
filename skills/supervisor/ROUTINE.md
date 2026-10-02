# The supervisor routine

This is the loop the supervisor session runs. The command blocks are code. Run
them as written. You notify only, so you never answer a run. You watch, and you
escalate every pause.

Each block starts a fresh shell, so each one names the work directory again.
Replace a `<placeholder>` with the value from the step before.

## Step 0: startup checks, once per session

Confirm that this checkout has no active run. If it has one, stop.

```bash
if delivery-loop status | grep -q '^No active delivery-loop run'; then echo "control checkout ok"; else echo "REFUSE: run the supervisor from a checkout with no active run"; exit 1; fi
```

Find the repository to watch, and keep it for the session. Without `gh`, or
outside GitHub, the name is empty and the scan watches the runs of every
repository. Each record then names its own `repo`.

```bash
S="${TMPDIR:-/tmp}/dl-supervisor"; mkdir -p "$S"
gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null > "$S/repo" || : > "$S/repo"
echo "repo: $(cat "$S/repo")"
```

Take the single-supervisor lock. Run this block in the background (the Bash
tool's `run_in_background`), because it holds the lock for as long as it runs.
It prints `LOCKED` and keeps running. If it prints `LOCK_HELD` and exits 1,
another supervisor watches this repository: stop. The name `all` stands for a
supervisor that watches every repository.

```bash
S="${TMPDIR:-/tmp}/dl-supervisor"; REPO=$(cat "$S/repo")
loop-ledger --repo "${REPO:-all}" lock
```

## Step 1: scan every run

Read every run's state. Exit `1` means at least one run needs attention. Exit
`0` means all clear. Exit `2` means the scan could not observe anything.

```bash
S="${TMPDIR:-/tmp}/dl-supervisor"; REPO=$(cat "$S/repo")
loop-scan ${REPO:+--repo "$REPO"} > "$S/scan.json"; echo "exit: $?"
```

Split the scan into one record file per run, and list the runs.

```bash
S="${TMPDIR:-/tmp}/dl-supervisor"; rm -f "$S"/record-*.json "$S"/question-*.json
python3 - "$S" <<'EOF'
import json, sys
from pathlib import Path
s = Path(sys.argv[1])
for n, r in enumerate(json.loads((s / "scan.json").read_text())):
    (s / f"record-{n}.json").write_text(json.dumps(r))
    print(n, r.get("run_id"), r.get("condition"), r.get("status"), r.get("session_id"))
EOF
```

## Step 2: read the hidden gates

Read the transcript of every run whose `condition` is `ok` and that has a
`session_id`, whatever its status. A pending `AskUserQuestion` is an approval
the state file cannot show. A `permission_prompt` outcome is a run parked on a
Claude Code permission prompt, such as a `git push` confirmation. It can sit on
any status, `done` included. Reading every transcript adds no false alarm: the
decider returns `noop` for a running run that is not stale.

```bash
S="${TMPDIR:-/tmp}/dl-supervisor"
loop-transcript --session-id "<session_id>" > "$S/question-<n>.json"
```

## Step 3: decide each run

Run the decider for each run, always with `--notify-only`. It returns
`escalate` or `noop`, with a `reason` and a `notify` line.

```bash
S="${TMPDIR:-/tmp}/dl-supervisor"
loop-decide --record-file "$S/record-<n>.json" --question-file "$S/question-<n>.json" --notify-only
```

When step 2 wrote no question for the run, leave out `--question-file`.

## Step 4: escalate

For each `escalate` decision, ask the ledger whether this pause needs a
notification on a channel. Use one channel name per way you send: `desktop`,
`tracker` or `session`. Exit 0 means send it. Exit 1 means this channel already
sent it less than 1800 seconds ago, or the run is `done` and was already
notified once: skip it.

```bash
S="${TMPDIR:-/tmp}/dl-supervisor"; REPO=$(cat "$S/repo")
loop-ledger --repo "${REPO:-all}" check --record-file "$S/record-<n>.json" --question-file "$S/question-<n>.json" --channel <channel>
```

Render the card the operator reads:

```bash
S="${TMPDIR:-/tmp}/dl-supervisor"
loop-card --record-file "$S/record-<n>.json" --question-file "$S/question-<n>.json"
```

Leave out `--question-file` when the run has no question. Add `--first-line`
to print only the notification line.

Then send it:

- If this session has the `PushNotification` tool, send the first line as one
  desktop notification.
- If this session has a connection to the issue tracker, and the record's
  `task` names an issue, post the full card as one comment on that issue.
- Otherwise, print the full card in this session.

After each send that succeeds, record it in the ledger, one line per channel.
The ledger lives under your home directory and survives a restart, so a new
supervisor session does not repeat a notification. A crash between the send and
the record can repeat one notification, never lose one.

```bash
S="${TMPDIR:-/tmp}/dl-supervisor"; REPO=$(cat "$S/repo")
loop-ledger --repo "${REPO:-all}" record --record-file "$S/record-<n>.json" --question-file "$S/question-<n>.json" --channel <channel>
```

In `check` and `record`, leave out `--question-file` when the run has no
question. Pass the same files to both, so both name the same pause.

For a `permission_prompt` decision, the card names the tool and the command,
and the reply is `Yes` or `No` to the prompt. For an `orphaned` decision, list
the runs whose worktree is gone, and tell the operator to delete them with
`loop-prune --yes`. You never run `--yes` yourself.

```bash
loop-prune
```

## Step 5: pace yourself

Wait, then run the loop again from step 1. Under `/loop`, schedule the next
turn with `ScheduleWakeup`. Do not scan more than once a minute. The ledger
decides when a pause is notified again: `check` says yes once 1800 seconds have
passed since the last notification on that channel.

## Stopping

When the operator ends the supervisor, release the lock. This ends the
background process that holds it. The lock is also freed when that process
dies.

```bash
S="${TMPDIR:-/tmp}/dl-supervisor"; REPO=$(cat "$S/repo")
loop-ledger --repo "${REPO:-all}" unlock
```
