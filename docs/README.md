# docs

The written half of the loop: the contract, and the runbooks an operator
follows.

## What belongs here

- **The contract.** `adapter-contract.md`: what a harness must provide, and the
  interface between an adapter and core.
- **Operator runbooks.** How to start a run, how to answer a pause, how to read
  the state file, how to recover a failed run, how to abort.
- The supervisor's escalation behaviour, and the decision table an operator
  reads at a pause.

## What does not belong here

- Code, prompt text, or configuration values.
- Anything specific to one host repository. A runbook describes the loop, not a
  project that uses it.

## Contents

| File | Read it when |
|---|---|
| `adapter-contract.md` | Writing or reviewing a harness adapter. |

The operator runbooks have not been written here yet. They exist alongside the
code that has not moved, and will follow it.
