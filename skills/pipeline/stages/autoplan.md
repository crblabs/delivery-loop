Run the stage's command on the task of this run, named in the header above. It
writes the plan file itself. Its final approval question is a question to the
person, not a turn end: when it is answered, the same turn continues.

The task does not change for the life of the run. At the final approval
question, any answer other than an approval is a constraint on the same task,
not a rejection of it: plan the same task again under that constraint. Read the
task's issue and every issue it links before you decide anything about scope,
never only their titles. If the constraint leaves the task with nothing to
deliver, stop for a person with a decision card that asks what the run should
deliver instead. Never pick another task, issue or backlog item.

Narrate nothing: one line per stage transition. The pull request body is the
only report. Ask a person nothing beyond the questions a gstack command asks
itself and the decision cards this prompt names. This run writes only to its
worktree and its pull request: no issue tracker writes, no issues, no comments,
and no writes outside the repository. Findings go in the pull request body, for
a person to file. An issue the run wants filed goes there as a draft that
`loop-issue <draft file>` accepts.

If the plan changes a loop file (a stage prompt under the repository's
`stages/` override directory, or another path the loop guards), list each path
in a fenced block opened with ```loop-edits, one path per line. A loop file the
plan does not declare stays locked for the rest of the run.

Do not edit code and do not commit in this stage. If the loop reinjects this
stage, read what the command already wrote before you run it again.
