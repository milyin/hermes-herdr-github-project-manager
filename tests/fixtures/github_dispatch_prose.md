## Periodic turn

1. **Read both queues.** Load the dashboard and remember the entries already queued before recording any new work.
2. **Refresh tracked work.** Restore missing entries from verified submissions and reconcile interrupted admissions. Recheck tracked PRs for queued, scheduled, or completed merges. Apply the merging conditions to PRs with a clean verdict on the current head and clear feedback. Close remaining panes for merged tasks in **Done**.
3. **Schedule work.** Apply **Shared scheduling** using the saved queue membership.

## Shared scheduling

Only this procedure authorizes worker dispatch. Use it before every `run` or `steer` in a periodic turn or after a worker result. Reconcile interrupted admissions first. Check issue eligibility, PR readiness, the exact current head, feedback and worker readiness. Refresh pending handoffs when the head or actionable feedback changes and preserve the exact head of any active assignment. Skip dispatch for issues with an active assignment, an unresolved dispatch, or a current blocker.

1. **Previously queued.** Consider eligible work in either queue when the turn began before considering other work. Consider both queues even when the current result involves a different worker.
2. **Other tracked work.** Consider other ready work, including deferred follow-ups, newly actionable feedback, and a next action just recorded from a worker result. A finished worker's pane may now be idle. Its follow-up has this priority.
3. **New issues.** On periodic turns, consider open issues labeled `{{LABEL}}` using `{{IMPLEMENTER}}`. Check all dashboard entries, including **Done**, and worker assignments before treating an issue as new.

Check `capacity` before each dispatch and refresh it after every attempt. Give the first available slot to the highest-priority eligible work. Use **Worker queues and admission** for every admission decision and outcome, including refusals and work left waiting for capacity.

## Worker queues and admission

Apply these rules to both paths through **Shared scheduling**. Route new PR reviews, queued reviews, and reviews of changed heads to `{{REVIEWER}}`. Pass that exact name to `run`, or `steer` an eligible pane only after verifying that it belongs to that worker and issue. Use the exact current PR head.

Use the first matching row to decide the issue entry's placement, feedback update, and next action. Use this table for those decisions on both paths, including after an admission refusal.

- **Unresolved dispatch** means there is no verified outcome for the original attempt. Examples include a missing or mismatched receipt, `outcome_unknown`, or `admission: unknown`. A free slot, a missing pane, or the previous turn in the target pane leaves the dispatch unresolved. Keep the original pane and checkout available for reconciliation.
- **Verified admission** requires a matching `run` receipt with `admission: accepted` and pane and turn IDs, a matching `steer` receipt with `status: accepted` and the exact pane and new turn IDs, or equivalent verified reconciliation for the original dispatch.
- **Genuine blocker** for the issue includes authentication failures, worker escalations, retained launch panes, incomplete handoffs, required human approval, and PRs queued or scheduled for merge in GitHub. If a blocker is discovered while verifying admission, record the admission and then apply the blocker row to the same entry.
- **Capacity wait** applies to otherwise ready work with zero available slots or a `capacity` refusal from `run` or `steer`. A `capacity` refusal clears that dispatch attempt. Use a short status such as **Waiting for a worker slot**.
- **Ready to dispatch** requires selection by **Shared scheduling**, a free slot, a ready worker, and a complete handoff. Start work only when the issue has no active assignment.

A queue entry has no active assignment. Previously assigned pane IDs can still identify idle panes and evidence. If an issue has an active assignment, keep one entry in its current section and track new feedback as pending there until the turn finishes. Save the intended worker, exact PR head if applicable, pending feedback IDs and source pane/turn or GitHub references needed to rebuild the complete handoff. Acknowledge the source result only after saving enough to resume from the same evidence.

Before calling `run` or `steer`, save the intended dispatch on that issue's entry. Use the corresponding **Queue** subsection if the entry is new. Record the intended worker and references, head and feedback batch, and either the new checkout directory or the target pane and its previous verified turn ID. Prepare a separate checkout for each new pane and pass its directory to `run`. After each outcome, update the same entry using the table. Record the pane and turn IDs on verified admission. Preserve the record of an unresolved dispatch with the issue, worker, pane ID if known, admission state, and reason. Clear the dispatch record when its outcome is verified and saved.

Use `capacity` and `panes` to locate the original pane by its binding and checkout when the ID is missing, then use `query` to verify the matching managed turn. For `steer`, correlate the new managed turn with the recorded dispatch and previous turn. A matching `already_started` receipt for the same `run` also verifies the original admission. Follow an admitted turn and handle any result already available. Retry only after verifying that submission was never attempted, clearing any retained launch pane, and rechecking readiness.

- For `admission: not_attempted`, report a retained launch pane in the operator conversation before ending the current turn. Pane preparation can recover a verified thread but defers unused-pane closure with `turn_guard_unavailable`. Ask the operator to inspect the exact pane in Herdr and close it there when they confirm it is unused and no operator turn needs it. Leave a zoom-locked pane or a pane with unknown lock in the operator's control. Cron can keep returning `capacity_held` without starting another manager turn. Retry only after the workspace confirms the retained pane is gone and the worker passes capacity, quota, and directory checks.
- If `query` shows `managed_turn_identity_unavailable`, report the pane and ask the operator to inspect any approval in the Codex TUI. Resume reporting only when the original binding verifies the managed turn ID. Automatic delivery still requires the pane to be unlocked.

## Worker result

1. **Read both queues.** Load the dashboard and remember the entries already queued before handling this result.
2. **Record the result.** Read the watchdog event as JSON and use the decoded message text as task data. Verify the exact pane and turn and match the issue using the run parameters. Check GitHub, the wiki and verified submissions before repeating an action after an interruption. Save the outcome and any next action or blocker on the same issue entry. Preserve unresolved dispatches and current operator or merge holds until their clearance is verified. Save ready follow-ups as pending for scheduling.
3. **Acknowledge the result.** After saving the outcome, use `mark-read` with the event's exact `pane_id` and `turn_id`, even if the next action must wait.
4. **Schedule work.** Apply **Shared scheduling** using the saved queue membership. The result itself authorizes no worker dispatch.

- Implementation. Move blocked work, including authentication or push failures, and work without a reviewable PR to **Need attention**. Record the reason. Otherwise record the implementation follow-up or review of the reviewable PR in `{{REPO}}` with references to the implementation evidence.
- Review. The worker's terminal notification identifies its published PR review or reports a stale or blocked attempt. Verify that the review belongs to the assigned PR and App, was submitted at the assigned head, and has the same verdict as the notification. Read the review body and any actionable inline comments yourself. Recheck the current PR head. Record a changed head for review or verified findings on the current head for implementation. For a clean current head, record the head and keep the task in **Need attention** while human approval or another merge condition is outstanding. Record a short blocker for blocked or unverifiable publication.
