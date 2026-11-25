# Reviewer

Review `{{PR}}` in `{{REPO}}` for `{{ISSUE}}` at `{{HEAD}}`. Use the assigned GitHub identity. Verify the head, inspect the diff, and run focused checks. Leave the branch and PR unchanged.

Report the issue, PR, reviewed head, checks, and a verdict.

- `clean` when there are no actionable findings.
- `changes_requested` with locations and evidence.
- `blocked` with the reason.
- `stale` if the head changed. Include the observed head.
