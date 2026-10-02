Ship the branch with the stage's command: it brings the branch up to date with
the base branch, runs the tests, pushes and opens the pull request. Push only
this run's branch, never force, and never rebase: bring the base in with a
merge. If the repository has no `VERSION` or `CHANGELOG.md`, the command must
not create them.

Look for an open pull request for this branch first, so a retry never pushes
twice: `gh pr list --head <branch> --state open --json url,headRefOid`. If one
exists, its head is `HEAD` and the base branch is already in `HEAD`, do not run
the command again: write its url and finish.

If this stage changed code after the review stage ended (a merge of the base
branch, a fix), run the review stage's command again here, with
`GSTACK_SESSION_KIND=spawned` on each preamble line, before you push. That work
is part of this stage.

Narrate nothing: one line per stage transition. The pull request body is the
only report. Ask a person nothing beyond the questions a gstack command asks
itself and the decision cards this prompt names. This run writes only to its
worktree and its pull request: no issue tracker writes, no issues, no comments,
and no writes outside the repository. Findings go in the pull request body, for
a person to file. An issue the run wants filed goes there as a draft that
`loop-issue <draft file>` accepts.

Before you push, run `loop-no-dash --base origin/<base branch>` and fix every
dash it names. Before you open the pull request, check its body with
`loop-no-dash --stdin-text --label "PR body" < <body file>` and fix it too.

Also run `loop-comments --base origin/<base branch>` and
`loop-complexity --base origin/<base branch>` before each commit, unless
`[checks]` in the loop configuration turns one off. Fix what each one names.
The stage cannot end while one of them fails.

If gstack's decision log is available, write the body's `## Decisions` section
with this command, and paste its output as it is. It prints `None` when the run
decided nothing.

    ~/.claude/skills/gstack/bin/gstack-decision-search --scope branch --json | loop-pr-decisions

Run `loop-impact --base origin/<base branch> --format both`. If the repository
has a zone map (`docs/architecture.zones.toml`), paste its output, as it is, as
the body's `## Picture` section. Do not draw the diagram by hand. If it says no
zone map is configured, the body has no `## Picture` section. If it names a
changed file that no zone owns, add that path to the zone map, commit, and run
it again. The zone map is an ordinary file of the repository.

Before you open the pull request, also check the body with
`loop-pr-body < <body file>`. If it names a missing section, add that section
and check again.

The body never restates the plan and never narrates the stages. It says, under
level-2 headings: the effect of the change in one sentence, what changed in at
most five lines, what ran to verify it, the findings for a person to file, the
decisions, and every question the run asked with its answer.

Write the pull request's url on its own line, as `PR: <url>`, before the token.
The stage ends over a clean worktree. After the token, do no more work in this
session: no merge, no commit, no push.
