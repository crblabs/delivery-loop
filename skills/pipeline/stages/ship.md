Ship the branch: bring it up to date with the base branch, run the full test
suite, push the branch to origin and open the pull request. Push only this
run's branch, and never force.

Before you push, run `loop-no-dash --base origin/<base branch>` and fix every
dash it names. Before you open the pull request, check its body with
`loop-no-dash --stdin-text --label "PR body" < <body file>` and fix it too.

If gstack's decision log is available, write the body's `## Decisions` section
with `gstack-decision-search --scope branch --json | loop-pr-decisions`. Paste
its output as it is. It prints `None` when the run decided nothing.

Before you open the pull request, also check the body with
`loop-pr-body < <body file>`. If it names a missing section, add that section
and check again.

Write the pull request's url on its own line, as `PR: <url>`, before the token.
The stage ends over a clean worktree.
