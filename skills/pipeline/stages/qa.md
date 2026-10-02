Test what the implement stage built, as a user of it would: run the app, the
CLI or the service, and walk the flows the plan names. Fix each bug you find
with a regression test, and commit each fix.

Run `loop-no-dash --base origin/<base branch>` before each commit. It fails on
an en dash, an em dash, a figure dash or a horizontal bar in a line this branch
adds. If it fails, replace each dash it names with a hyphen, a comma, a colon or
a new sentence, then run it again. Check each commit message the same way before
you commit:
`loop-no-dash --stdin-text --label "commit message" < <message file>`.

The stage ends over a clean worktree. If a bug needs a product decision, stop
for a person with a decision card.
