Implement the approved plan named above, and nothing outside it. Write the
tests the plan asks for alongside the code. Run the project's own test and lint
commands until they pass.

Run `loop-no-dash --base origin/<base branch>` before each commit. It fails on
an en dash, an em dash, a figure dash or a horizontal bar in a line this branch
adds. If it fails, replace each dash it names with a hyphen, a comma, a colon or
a new sentence, then run it again. Check each commit message the same way before
you commit:
`loop-no-dash --stdin-text --label "commit message" < <message file>`.

If the repository has `.comments-policy.json` at its root, also run
`loop-comments --base origin/<base branch>` before each commit. If it has
`.complexity-baseline.json`, also run
`loop-complexity --base origin/<base branch>`. Fix what each one names. The
stage cannot end while one of them fails.

Commit the work on this branch with messages that say what changed and why. The
stage ends over a clean worktree: no uncommitted changes.

If the plan turns out to be wrong in a way you cannot settle from the code,
stop for a person with a decision card instead of guessing.
