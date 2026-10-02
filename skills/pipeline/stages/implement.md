Implement the approved plan named above, and nothing outside it. Build the
whole plan, not a demo path: every edge case, error path and test the plan
names. If you find more scope, note it for the pull request body and do not
build it. Run the project's own test and lint commands until they pass. Apply
the formatter first: a check that only reports formatting does not fix it.

Narrate nothing: one line per stage transition. The pull request body is the
only report. Ask a person nothing beyond the questions a gstack command asks
itself and the decision cards this prompt names. This run writes only to its
worktree and its pull request: no issue tracker writes, no issues, no comments,
and no writes outside the repository. Findings go in the pull request body, for
a person to file. An issue the run wants filed goes there as a draft that
`loop-issue <draft file>` accepts.

Decide, do not ask, on a reversible choice. Classify each choice first. A
choice that is reversible and does not change what a person receives is yours:
take the option you can defend, record it, and continue. Record it, so the
ship stage can print it under `## Decisions`:

    ~/.claude/skills/gstack/bin/gstack-decision-log \
      '{"decision":"<the choice and the option taken>","rationale":"<why>","scope":"branch","source":"skill"}'

Only a choice that changes the result, or that cannot be undone (a delete, a
force push, an overwrite), stops for a person with a decision card. That stop
wins over any default a gstack command would take.

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

Commit the work on this branch with messages that say what changed and why. The
stage ends over a clean worktree: no uncommitted changes.

If the loop reinjects this stage, do not start over. Read `git status` and
`git log` first, keep the commits that exist, and finish what is left.
