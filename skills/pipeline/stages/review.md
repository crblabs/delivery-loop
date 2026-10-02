Review the branch's whole diff against the base branch before it lands: look
for correctness bugs, missing tests, security problems and anything the plan
promised that the code does not do. Fix what you find and commit the fixes.

Run `loop-no-dash --base origin/<base branch>` before each commit. It fails on
an en dash, an em dash, a figure dash or a horizontal bar in a line this branch
adds. If it fails, replace each dash it names with a hyphen, a comma, a colon or
a new sentence, then run it again. Check each commit message the same way before
you commit:
`loop-no-dash --stdin-text --label "commit message" < <message file>`.

Also run `loop-comments --base origin/<base branch>` and
`loop-complexity --base origin/<base branch>` before each commit, unless
`[checks]` in the loop configuration turns one off. Fix what each one names.
The stage cannot end while one of them fails.

When the review raises questions only a person can answer, collect them and
stop once with a decision card that asks them together. The stage ends over a
clean worktree.
