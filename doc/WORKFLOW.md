# Hermes Herdr project manager workflow

This specification defines the target behavior. [Profile settings](PROFILE.md) define the manager and named workers. The [Hermes commands API](COMMANDS.md) exposes worker, inspection and watchdog operations.

## Workflow and engine

The **workflow** is the set of user-selected manager and worker prompts. It defines how to find work, assign it, act on results, and track project progress. It chooses its own data sources, services, and storage. The [GitHub workflow](MANAGER_PROMPT.md) is an example that uses a wiki dashboard. Other workflows can use different project data and storage.

The **engine** is the project-agnostic part of the installable skill and plugin. It renders prompts, prepares panes, tracks turns, enforces quota and concurrency, and delivers worker results. Its input contract treats prompt text, parameters, pane comments, and results as opaque data. Herdr's live pane list is the source of truth for which worker panes the engine tracks. Each pane label's status field holds its delivery state. The engine retains the manager's conversation and exact Herdr binding so it can reopen the same conversation when its pane is replaced. The engine reads worker bindings, exact turns, results, and receipts through the dependency APIs.

The manager runs the workflow in a Hermes gateway session displayed in the terminal UI (TUI) in a dedicated Herdr pane. It calls the [project-manager tools](COMMANDS.md#manager-tools) to delegate and inspect workers. The engine uses the pane manager for layout and the agent manager for worker sessions and quota.

Provide one implementation for each operation and use it from Hermes commands, manager tools and cron. They share the same profile binding, observations, guards and result fields. In particular, manual inspection and cron use the same pane, quota, and capacity queries.

## Manager conversation

Open the selected profile's Hermes TUI in the manager pane. Group it with the workspace's initial shell when layout permits so zoom is available from first launch. The profile gateway runs the turns and owns the conversation. The TUI lets the operator view the transcript and talk to the manager. The watchdog connects through the authenticated local gateway interface to submit scheduled instructions and worker results into that same conversation. [Hermes TUI gateway](https://hermes-agent.nousresearch.com/docs/user-guide/tui#how-the-tui-talks-to-its-gateway)

Bind the profile, exact Herdr pane, and gateway conversation before enabling scheduled turns. Verify that the live TUI is attached to the bound conversation and that the gateway has applied the configured working directory, model, and any launch profile to the session. Hold scheduled turns while the TUI is suspended, disconnected, or displaying another conversation. Zoom the manager pane to take control and hold new watchdog messages. When a replacement pane is needed, reattach the TUI to the same gateway conversation. Create a fresh conversation only after the gateway confirms the previous one is gone. Worker results remain available through their native session history and their pane statuses still govern delivery.

### Gateway requirements

- Verify the TUI attachment and read `running` and `pending` for the exact bound session from the shared gateway session snapshot without starting a model turn or replacing the TUI attachment. Apply a requested manager launch profile to the gateway session's tool execution environment. Refuse setup if the requested execution profile cannot be applied.
- Use the shared gateway's native `prompt.submit` queue with a `submission_id` for each watchdog submission. If operator input arrives first after the idle check, the watchdog message queues behind it in the same conversation. Use native gateway state and admission receipts to observe submissions. [Hermes shared gateway API](https://hermes-agent.nousresearch.com/docs/developer-guide/programmatic-integration#which-methods-the-shared-gateway-answers)

## Engine instructions for the manager

The engine registers its tools and supplies their names and argument schemas to the bound gateway session. Verify the session binding when dispatching manager tools. Register the following fixed instructions through Hermes's system-prompt section API before the manager's first turn. The workflow's manager prompt supplies the project rules. [Hermes system-prompt sections](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks#cache-safe-system-prompt-sections)

> Use only the registered project-manager tools to start or steer workers, inspect or acknowledge reports, change pane comments, and close worker panes. Use `project_manager_run` to start a worker, `project_manager_steer` for a follow-up, `project_manager_query` to inspect a result, `project_manager_mark_read` to acknowledge a report, `project_manager_set_comment` to change a pane comment, and `project_manager_close` to close a pane. Leave pane preparation and report delivery to the engine. If Hermes defers these tools, use Tool Search with the exact names and invoke them through `tool_call`.

## Interactive Codex workers

Give each worker one Codex TUI connected to a private app server. Start it with the worker's launch profile if configured and let the actual TUI establish the thread without starting a model turn. Verify the TUI attachment to that thread before sending the first task. Route operator and automated turn admissions through the same receiver so they share an idle check and an admission lock. Route requests for approval or input to the TUI. [Codex App Server](https://developers.openai.com/codex/app-server)

The operator can read the job transcript, ask questions, give instructions, and respond to approval requests in the pane using the Codex TUI. Zoom the pane to take control. While zoomed, hold automated submissions and result delivery for that worker. A running turn can continue, and the operator can steer or interrupt it through Codex. The app server applies the worker's configured execution profile to turns from both clients. Deliver approval requests to the TUI even for turns started by the agent manager, so the operator can decide.

After unlock, verify the attachment and thread state before resuming automation. A managed pane accepts work on its original thread for its lifetime. Use normal Codex panes for operations that replace, fork, rewind, or run a separate review of that thread. Count a turn started by either client as working. Admit automated follow-ups only while the bound thread is idle. Completed operator turns stay in the transcript and leave the thread eligible for follow-up. Preserve the exact result of each automated turn, including any instructions the operator steered into that turn. To send Hermes an updated result after manual work, unlock the pane and use `steer` to request a fresh report.

## Pane lifecycle

Use the same scripted workspace and pane verification on first launch and on each cron tick. Find the managed workspace in Herdr and verify its exact ID. Create it through the pane manager when none is bound. Scope each pane to its exact Herdr instance, workspace ID and pane ID. Verify the binding and read turn IDs and named run parameters through the corresponding session API. If the binding or the outcome of an operation is ambiguous, hold the affected pane until its state can be verified.

### Pane labels

Show the short pane ID, status, and optional single-line comment in that order, for example `w1:p2 I:RUN #42:#27`. Keep the native ID as the address for API operations. The pane manager owns the displayed ID, and exposes status and comment as separate fields. The engine sets status and includes the worker's configured `pane_tag`. The workflow uses `set-comment` to supply the comment.

| Status | Meaning |
| --- | --- |
| `MGR` | Hermes manager pane. The gateway supplies the turn state. |
| `QUOTA` | Codex quota receiver. |
| `<tag>:START` | A worker pane created for its first submission. The admission field distinguishes an unused launch from a submission awaiting verification. |
| `<tag>:RUN` | A worker turn is being submitted or is running. |
| `<tag>:UNREAD` | A finished result awaits workflow acknowledgement. |
| `<tag>:READ` | The workflow acknowledged the exact worker result. |
| `<tag>:WAIT:<attention_id>` | An escalation awaits workflow acknowledgement. |
| `<tag>:ACK:<attention_id>` | The workflow acknowledged that escalation. The pane still uses its working slot. |
| `<tag>:?` | The engine cannot verify the pane or turn outcome. |

Use the short attention ID in the status, for example `I:WAIT:7`, so the same turn can have distinct escalations. Read the full worker name and native turn ID from the session binding. Verify label updates against the current binding. Confirm that the exact result is retrievable before marking a pane unread. The workflow decides when to close worker panes. Closure ends result delivery for that pane. An unresolved initial submission can outlive its pane.

Treat a zoom-locked pane as `working` in all scheduling and capacity decisions. Check zoom immediately before a prompt, result delivery, or ordinary label update. Hold the operation when the pane is already zoomed. If the operator zooms just after the check, a message can land or a label-only update can complete. A present worker with an unknown lock still counts against capacity. Hold operations that require an unlocked or idle pane until its state is verified.

### Starting a worker turn

`run` creates a `<tag>:START` pane. Provisioning failures can leave this pane visible with `admission: not_attempted`. Before the engine sends the initial prompt, it verifies the unlocked pane and idle thread and records the workspace, pane, terminal, worker, thread and fingerprints of the dispatch and named parameters. The record is private to the profile and contains no prompt text. A verified Codex rejection or a receiver deadline before the prompt write releases the submission record and follows guarded cleanup. A timeout after a possible write leaves the submission on hold.

Pane preparation can recover the same native thread when the agent manager reports it is recoverable. It closes a verified unused pane through the guarded close operation. If the receiver, terminal, idle state or zoom lock cannot be verified, the pane stays in place and continues to occupy capacity. A missing or unverified admission receipt keeps the dispatch on hold until the original binding identifies the admitted turn. Loss of the pane releases pane capacity after absence is verified, with the dispatch still held against replay. A legacy `RUN` pane without an identified turn has an unknown initial admission.

The engine records the admitted turn ID before updating the pane label. The recorded ID lets preparation send attention for the exact managed turn even if a previous label update was refused. When there is no recorded turn ID, an attention snapshot can identify operator work. The engine requires matching run parameters from the exact native turn before adopting attention from that snapshot. If the receiver has yet to expose that evidence, the submission remains on hold.

Before `steer` submits a follow-up, set and verify `<tag>:RUN` on the bound pane. Serialize the update and submission with pane preparation and `mark-read`. After a definitive refusal, restore the prior status if the binding and previous turn still match. After an interruption, use native turn state and receipts before retrying. A verified finished turn beside `RUN` becomes `UNREAD`.

### Deferred close

The pane manager checks zoom explicitly when closing a verified pane. For automatic closure, require a native close operation that checks the exact pane and its tab when applying the close. If zoom blocks closure or the native operation is unavailable, the pane manager adds `DEAD` to the displayed pane ID and returns `close_deferred` with the reason. For example, `w1:p2:DEAD I:READ #42:#27` preserves the status and comment. The mark can be set while zoomed. The native Herdr ID remains the address for operations. If the lock is unknown, hold the close until it can be verified.

The pane manager preserves the `DEAD` mark through status and comment updates and exposes pending closure in its layout records. The operator can still use the pane interactively. The engine uses the pane manager's pending-closure field to skip submissions and result delivery. During pane preparation, it retries the native guarded close for verified idle panes. When the server lacks that operation, the operator can close the marked pane through Herdr after use. Pending closures do not delay other eligible work.

Determine manager turn state through the Hermes gateway and worker state through the agent manager. Distinguish a running turn (`working`), an agent waiting for input or approval (`blocked`), and a bound pane process stopped by the operating system (`suspended`). A suspended manager TUI prevents scheduled delivery even while the gateway keeps running. A `done` result requires a completion receipt for the exact turn. With missing or conflicting evidence, report `unknown`. Hold new submissions to blocked, suspended, and unknown sessions. An unfinished worker turn continues to occupy capacity while blocked or suspended. Use Herdr's UI agent status as evidence for a hold and the bound session to verify readiness and turn completion. [Herdr status detection](https://herdr.dev/tui/docs/agents/#how-status-works)

The agent manager handles recoverable worker problems in the bound session. For example, it can reconnect to the same app server and verify the thread and turn before resuming observation. Escalate when recovery cannot proceed or a person needs to respond, including unanswered approvals and operator suspensions. Preserve the pane and checkout. When the worker backend confirms an interruption, deliver the interruption for the verified open pane and turn.

## Cron tick

Run one no-agent watchdog job per enabled profile. Each tick reads the profile and exits before touching panes or turns if `enabled` is missing or false. Each enabled tick follows this order:

1. Use the manager portion of `prepare-panes` to verify the workspace and recover the manager pane when necessary. Use the pane query for its effective state. End this tick unless the TUI is attached to its unlocked pane and the gateway confirms the bound session is idle with no pending admissions.
2. Use the worker portion of `prepare-panes` to retry deferred closes, verify worker completions and escalations through the agent manager, and update pane statuses.
3. Use `quota`. If the quota gate holds, end this tick.
4. Use the pane query to select the oldest unread worker report, either a finished result or an escalation. Submit it through the gateway and end this tick.
5. If there is no unread worker report, use `capacity` to verify that at least one configured worker is ready to start a run. End this tick if none qualifies.
6. Submit a `periodic` event through the gateway with the configured manager prompt so the workflow can discover work, then end this tick.

Worker reports take priority over the capacity check. A quota hold delays both worker reports and discovery turns. Serialize watchdog selection and submission with pane mutations for the same profile. While the manager has active or pending input, skip worker-report reads and submissions on subsequent ticks. Release the lock after the submission attempt so the manager can call engine tools during the turn.

A worker is ready to start when a slot is available, no unresolved launch conflicts with it, the agent manager verifies its backend, model and launch profile prerequisites, and its quota allows submission. This preflight uses script only. Validate the selected working directory and recheck capacity and quota at submission.

Immediately before submitting to the bound conversation, recheck the TUI attachment, zoom lock, and gateway state. End the tick if the pane is locked or the conversation has active or pending input. If operator input or zoom changes after the check, one watchdog message can still land in the conversation. If the submission reply is lost, end the tick. On the next tick, check the gateway state again before selecting work.

If Hermes reports an `unknown` watchdog admission at the head of the manager queue, resolve it through the gateway before selecting work. Leave unknown operator admissions for the operator. Worker delivery still follows the pane's acknowledgement state.

## Deliver a worker result

Send a `worker_result` event with `worker`, `pane_id`, the native `turn_id` and `message`. If the full result exceeds the input limit, send a bounded excerpt with `truncated: true`. The full result is available through `query PANE_ID --turn TURN_ID`. Present the result as data alongside the configured manager prompt.

Identify a result by workspace, worker pane ID and worker turn ID. A gateway admission confirms submission. The pane remains unread until the workflow acknowledges the result or the pane moves on to another turn or closes. The result remains eligible for delivery on a later tick once the manager is idle and has no pending admissions. Repeated delivery of the same result is allowed.

The engine delivers pending worker results for open panes. The workflow can act on information from any source and calls `mark-read` for the exact pane and turn after handling the result. The engine changes the status only if the pane still represents that turn. Accept `steer` on any eligible idle pane, regardless of prior delivery or acknowledgement. A successful `steer` supersedes the previous result for delivery. The previous result remains available through native turn history while the pane is open.

### Worker attention

Send a `worker_attention` event with `worker`, `pane_id`, `turn_id`, `attention_id`, `state` (`blocked` or `suspended`), and the agent manager's reason. Use the same delivery and retry rules as for a worker result, with `attention_id` distinguishing separate blocks on the same turn. The workflow uses the report to surface the need for operator action and acknowledges the exact escalation with `mark-read`. The engine leaves the turn active and counts its pane against capacity after acknowledgement. When the agent manager reports that the block has cleared or the turn has finished, retire the escalation and update the pane from the verified turn state.

## Quota and capacity

Count working or possibly working panes against the worker limit, including zoom-locked panes. An unlocked idle `READ` pane leaves a slot available for another run. `steer` on an idle pane requires a free slot, regardless of its delivery state. Reserve capacity atomically with automated worker admission. Operator turns remain interactive and count against subsequent automated admissions, even when manual activity takes usage above the limit.

Use separate working directories for retained panes that the operator can reactivate. `run` accepts a directory chosen by the workflow and uses the configured default when omitted. Validate the resolved directory at launch and refuse a conflicting use of the same directory. The workflow prepares any project-specific checkout before invoking `run`.

On each tick, read valid rate-limit windows from the agent manager for each distinct account used by configured Codex workers. An account-only probe can read windows without opening a worker pane or sending a prompt. The worker receiver verifies a fresh reading for that same account before accepting a turn. When there are no Codex workers, the Codex quota check is skipped.

The rule is fixed for each current window. The usage allowance equals the fraction of time elapsed in that window. At 40% elapsed, 40% usage is allowed and 41% usage holds. Hold when any usable window is above its allowance or a required account has no usable observation. Check the selected account again for each new Codex submission and honor provider limits. Recheck capacity on both `run` and `steer` because a periodic manager turn may issue commands after cron has finished checking it.

## Dependency skill requirements

Use the skills' public caller APIs. The engine owns pane roles, result delivery, and the rule that a zoom-locked pane counts as working.

### Herdr pane manager

The pane manager supplies workspace and pane identity, layout, and guarded mutations. [Herdr pane manager skill](https://github.com/milyin/hermes-herdr-pane-manager) · [Caller API](https://github.com/milyin/hermes-herdr-pane-manager/blob/main/references/api.md)

- Use `workspaces` and `workspace-create` in the configured local Herdr instance. `layout` must expose the native pane and terminal IDs, displayed ID, status, comment, zoom state and pending closure separately. Use `lock-status` to verify the target's lock before operations. Use `process-info` to identify the retained pane of an exited manager launcher. A structured label appears in the native creation request so an interrupted creation remains visible for reconciliation.
- Use `create --argv` with the manager working directory and status to launch the Hermes TUI. Worker panes are created by the agent manager during `provision` with the tagged `START` status. Retain any pane ID from a partial or uncertain creation for reconciliation.
- Require `create` and `rename` to take separate status and comment fields. Use `rename` to update either field while preserving the other field and the pane manager's displayed ID. Serialize label changes for the same pane. An empty comment clears it. Return both fields in `layout` and keep ordinary label updates subject to the zoom check.
- Use `close --defer-locked` for project-manager closure. The guarded native operation receives the exact pane, terminal and workspace and checks the tab zoom state when closing. For `user_locked`, `tab_user_locked` or `guarded_close_unavailable`, mark the displayed ID `DEAD` and return `close_deferred` with the native pane ID. For `lock_unknown`, hold until the lock can be verified.

### Herdr agent manager

The agent manager supplies worker sessions, native worker turn operations, and Codex quota readings. [Herdr agent manager skill](https://github.com/milyin/hermes-herdr-agent-manager) · [Caller API](https://github.com/milyin/hermes-herdr-agent-manager/blob/main/references/api.md)

- Use `provision` with the backend and working directory, and `run --model` with the selected model. Require a scripted launch preflight that verifies the configured agent, model, working directory, and any requested launch profile without opening a worker pane or invoking a model. Pass the launch profile reference through unchanged. The agent manager applies that profile to the pane and refuses the launch if it cannot apply it. Keep credentials out of prompts and receipts.
- For Codex, `provision` must establish and report the binding between the private app server, native thread ID, and interactive worker pane. The TUI displays turns submitted by the agent manager, and the agent manager observes turns and state changes initiated through the TUI. Honor the worker's approval policy. Route requests requiring an operator decision to the TUI even when the agent manager started the turn. Hold automation while the binding cannot be verified. [Interactive Codex workers](#interactive-codex-workers)
- Require a machine-readable status query for the exact bound session, including before its first turn. Use native backend state and bound process state to distinguish idle, working, blocked, suspended and unknown. Use `wait` with the exact known turn ID to observe completion. Expose results and named run parameters by exact thread and turn through native session history for bound open panes. Distinguish confirmed failure or interruption from an unknown outcome.
- Recover routine worker failures through the verified session when possible. Resolve ambiguous submissions before retrying. Leave approvals and requested input to the operator. For a block that needs escalation, expose its state, reason, observed time, and a short, label-safe `attention_id` unique within the pane and turn. Keep that ID through repeated observations of the same block. Report when the block clears.
- Use idle `steer` bound with `--thread-id`. Enforce the lock check before submission and require the app-server thread to be idle when it admits the turn. If operator input starts a turn first, refuse the automated submission. Both `run` and `steer` return the native thread and turn IDs as soon as the turn is admitted. Use `wait` separately for completion. Distinguish a definitive refusal from an unknown outcome.
- Use account-bound Codex quota readings for remaining percentage, window duration and reset time. Verify the account again inside the worker receiver before submission. Report unavailable or unverified observations so the project manager can hold the affected work.
