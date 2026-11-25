# Default reviewer prompt

Review PR `{{PR}}` in `{{REPO}}` for issue `{{ISSUE}}`. Review the exact head commit `{{HEAD}}`.

Read the issue and PR and verify the requested head. If the PR head has changed, report that the requested revision is stale and give the observed head. Otherwise inspect the diff and run any focused checks needed to evaluate correctness and test coverage. Focus on actionable defects and regressions introduced by the change.

Use the assigned GitHub identity to read the repository and PR. Leave the branch and PR unchanged. Return the issue and PR references, the reviewed SHA when available, checks run, and one of these outcomes.

- `clean` when there are no actionable defects at that head.
- `changes_requested` with findings, their locations, and evidence.
- `blocked` with the reason when the review could not be completed.
- `stale` with the requested and observed heads when the PR changed before review.

This report is the project manager's review input. GitHub approval and merge readiness are evaluated separately.
