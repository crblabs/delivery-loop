---
description: Drive the delivery loop in this worktree - start <task>, status, resume, abort, doctor
argument-hint: start <task> | status | resume | abort | doctor
disable-model-invocation: true
allowed-tools: Bash(delivery-loop:*)
---

```!
delivery-loop --session "${CLAUDE_SESSION_ID}" --args-stdin 2>&1 <<'DELIVERY_LOOP_ARGS_7C1F9E2B'
$ARGUMENTS
DELIVERY_LOOP_ARGS_7C1F9E2B
```

The block above is the output of `delivery-loop $ARGUMENTS`.

- If it contains a line starting with `Stage ` followed by a number, a stage is
  starting: do that stage now, following its instructions and the `pipeline`
  skill of this plugin.
- If a `delivery-loop resume:` note follows, the run resumed: do the stage it
  names now. A `delivery-loop abort:` note means the run ended.
- Otherwise, show the output to the person as it is, in a code block, and stop.
