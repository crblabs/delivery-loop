Make sure that the change does what the plan says. Run the project's own
checks first, and fix them until they pass. Then, when the change has something
a user can run (an app, a page, a CLI or a service), test it as a user would
and walk the flows the plan names.

Run the stage's command with `GSTACK_SESSION_KIND=spawned` set on the same Bash
line as each of its preamble commands, so its own two-way questions take the
recommended option instead of stopping. Shell variables do not survive between
Bash calls, so set the prefix on every preamble line, not once. Fix each bug it
finds with a regression test, and commit each fix.

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

The stage ends over a clean worktree. If the loop reinjects this stage, do not
repeat a check that already passed and committed.
