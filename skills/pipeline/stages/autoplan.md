Plan the task before any code changes. Read the task, the repository and its
docs, and run the stage's planning command. Settle every open question the plan
raises, then write the approved plan to one file.

If the plan changes a loop file (a stage prompt under the repository's
`stages/` override directory, or another path the loop guards), list each path
in a fenced block opened with ```loop-edits, one path per line. A loop file the
plan does not declare stays locked for the rest of the run.

Do not edit code in this stage.
