# Hello-world manager

Use the configured `hello` worker to demonstrate delegation and result delivery locally.

- On a periodic turn, inspect the `hello` worker's panes. If none exists and this conversation has no recorded demo result, run `hello` with the literal task `Say hello and report that the worker received its task.` Keep the pane open so the operator can inspect it.
- When the worker returns a result, show its greeting and pane ID to the operator, then acknowledge the exact turn with `project_manager_mark_read`.
- If the worker needs operator attention, report the reason and pane ID and acknowledge the attention ID.

Use only the local project-manager tools. The demo needs no Git or GitHub operations.
