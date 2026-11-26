# Hermes Herdr project manager workflow

This specification defines the target behavior. [Profile settings](PROFILE.md) define the manager and named workers. The [Hermes commands API](COMMANDS.md) exposes worker, inspection and watchdog operations.

## Workflow and engine

The **workflow** is the set of user-selected manager and worker prompts. It defines how to find work, assign it, act on results, and track project progress. It chooses its own data sources, services, and storage. The [GitHub workflow](MANAGER_PROMPT.md) is an example that uses a wiki dashboard. Other workflows can use different project data and storage.

The **engine** is the project-agnostic part of the installable skill. It renders prompts, prepares panes, tracks turns, enforces quota and concurrency, and delivers worker results. Its input contract treats prompt text, parameters, and results as opaque data, independent of the workflow's services and storage. Herdr workspace and pane IDs identify managed panes, and pane suffixes hold the engine's persistent status. The engine reads session bindings, exact turns, results, and receipts through the native APIs. These are its only sources of persistent operational state.

The manager runs the workflow in a Hermes gateway session displayed in the terminal UI (TUI) in a dedicated Herdr pane. It calls the [project-manager tools](COMMANDS.md#manager-tools) to delegate and inspect workers. The engine uses the pane manager for layout and the agent manager for worker sessions and quota.

Provide one implementation for each operation and use it from Hermes commands, manager tools and cron. They share the same profile binding, observations, guards and result fields. In particular, manual inspection and cron use the same pane, quota, and capacity queries.

## Manager conversation

Launch `hermes -p PROFILE --tui` in the manager pane. The selected profile's Hermes gateway runs the turns and owns the conversation. The TUI lets the operator view the transcript and talk to the manager. The watchdog connects through the authenticated local gateway interface to submit scheduled instructions and worker results into that same conversation. Use the gateway interface for state and admission. [Hermes TUI gateway](https://hermes-agent.nousresearch.com/docs/user-guide/tui#how-the-tui-talks-to-its-gateway)

Bind the profile, exact Herdr pane, and gateway conversation before enabling scheduled turns. Verify that the live TUI is attached to the bound conversation and that the gateway has applied the configured working directory, model, and any launch profile to the session. Hold scheduled turns while the TUI is suspended, disconnected, or displaying another conversation. When a replacement pane is needed, reattach the TUI to the same gateway conversation. Create a fresh conversation only after the gateway confirms the previous one is gone and the engine reconciles any outstanding submissions. Worker results remain available through their native session history.

### Gateway requirements

- The gateway must let the engine verify the TUI attachment and query the exact bound session's state without starting a model turn. Apply a requested manager launch profile to the gateway session's tool execution environment. Refuse setup if the environment and credentials cannot be applied. Keep credentials scoped to the session and its tool processes.
- For watchdog messages, the gateway must check that the bound session is idle and accept its next turn in one operation. If an operator turn or a queued turn already occupies the next place, return a definitive refusal. Leave the watchdog message unqueued for a later tick.
- The gateway must record admission in its conversation history under a caller-supplied event ID before executing the turn. Return a receipt identifying the conversation and accepted turn. Reusing the same ID for the same message returns the original receipt. Refuse reuse with different content. Support receipt lookup in that history after gateway restart.

## Engine instructions for the manager

The engine registers its tools and supplies their names and argument schemas to the bound gateway session. Verify the session binding when dispatching manager tools. Register the following fixed instructions through Hermes's system-prompt section API before the manager's first turn. The workflow's manager prompt supplies the project rules. [Hermes system-prompt sections](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks#cache-safe-system-prompt-sections)

> Use only the registered project-manager tools to start or steer workers, inspect or acknowledge reports, and close worker panes. Use `project_manager_run` to start a worker, `project_manager_steer` for a follow-up, `project_manager_query` to inspect a result, `project_manager_mark_read` to acknowledge a report, and `project_manager_close` to close a pane. Leave pane preparation and report delivery to the engine.

## Interactive Codex workers

Give each Codex worker one live app-server thread. The agent manager starts the app server with the worker's launch profile, opens a thread without submitting a turn, and starts a Codex TUI in the worker pane attached to that exact thread. Use a private local connection, such as `codex --remote unix://SOCKET resume THREAD_ID` on a system with Unix socket support. The agent manager submits work and observes turn events through the same app server. Verify the attachment before sending the first task. [Codex App Server](https://developers.openai.com/codex/app-server) · [Codex TUI session client](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/tui/src/app_server_session.rs)

The operator can read the job transcript, ask questions, give instructions, and respond to approval requests in the pane using the Codex TUI. Zoom the pane to take control. While zoomed, hold automated submissions and result delivery for that worker. A running turn can continue, and the operator can steer or interrupt it through Codex. The app server applies the worker's configured execution profile to turns from both clients. Deliver approval requests to the TUI even for turns started by the agent manager, so the operator can decide.

After unlock, verify the attachment and thread state before resuming automation. If the operator has switched threads, hold until the TUI shows the original thread again. Count a turn started by either client as working. Require automated follow-ups to start only if the verified thread is still idle and its last turn matches the one observed by the caller. Refuse if operator input has changed the thread in the meantime. Preserve the exact result of each automated turn, including any instructions the operator steered into that turn. Later operator turns stay in the transcript. To send Hermes an updated result after manual work, unlock the pane and use `steer` to request a fresh report.

## Pane lifecycle

Use the same scripted workspace and pane verification on first launch and on each cron tick. Find the managed workspace in Herdr and verify its exact ID. Create it through the pane manager when none is bound. Scope each pane to its exact Herdr instance, workspace ID and pane ID. Verify the binding and read turn IDs and named run parameters through the corresponding session API. If the binding or the outcome of an operation is ambiguous, hold the affected pane until its state can be verified.

The pane manager owns the ID prefix of the pane label. The engine supplies the application suffix that follows it.

| Suffix | Meaning |
| --- | --- |
| `pm-manager` | The Hermes TUI attached to the manager conversation. The gateway supplies the turn state. |
| `pm-quota` | A dedicated quota receiver when one is needed to read Codex account limits. |
| `pm-worker:<name>:working` | The latest turn is starting or running. |
| `pm-worker:<name>:unread` | The tracked worker turn finished and its result is pending workflow acknowledgement. |
| `pm-worker:<name>:read` | The workflow acknowledged the exact worker turn. |
| `pm-worker:<name>:attention:<attention_id>:unread` | The agent manager escalated a block or suspension on the current worker turn. The report awaits workflow acknowledgement. |
| `pm-worker:<name>:attention:<attention_id>:read` | The workflow acknowledged that escalation. The pane still uses its working slot. |
| `pm-worker:<name>:unknown` | The engine cannot verify the pane or turn outcome. |

Verify suffix updates against the current binding. Confirm that the exact result is retrievable before marking a pane unread. Use the dependency APIs to verify suffix state after a restart or a failed rename. For a closed pane whose binding can be verified, use native turn history for the result. The workflow decides when to close worker panes. The engine verifies that a requested close is safe.

Treat a zoom-locked pane as `working` in all scheduling and capacity decisions. Check zoom immediately before a prompt, result delivery, or ordinary suffix update. Hold the operation when the pane is already zoomed. If the operator zooms just after the check, a message can land or a label-only update can complete. A present worker with an unknown lock still counts against capacity. Hold operations that require an unlocked or idle pane until its state is verified.

### Deferred close

The pane manager checks zoom explicitly when closing a verified pane. Herdr must check the zoom lock again when applying the close so a newly zoomed pane stays open. If the target pane or a sibling in its tab is zoomed, the pane manager adds `DEAD` to the displayed pane ID in the label prefix and returns `close_deferred` once the mark is verified. The mark can be set while zoomed. The native Herdr ID remains the address for operations, and the application suffix stays intact. If the lock is unknown, hold the close until it can be verified.

The pane manager preserves the `DEAD` mark through application suffix updates and exposes pending closure in its layout records. The operator can still use the pane interactively. The engine uses the pane manager's pending-closure status to skip submissions and result delivery. During pane preparation, it asks the pane manager to retry pending closes for verified idle panes. The pane manager checks zoom again and confirms closure.

Determine manager turn state through the Hermes gateway and worker state through the agent manager. Distinguish a running turn (`working`), an agent waiting for input or approval (`blocked`), and a bound pane process stopped by the operating system (`suspended`). A suspended manager TUI prevents scheduled delivery even while the gateway keeps running. A `done` result requires a completion receipt for the exact turn. With missing or conflicting evidence, report `unknown`. Hold new submissions to blocked, suspended, and unknown sessions. An unfinished worker turn continues to occupy capacity while blocked or suspended. Use Herdr's UI agent status as evidence for a hold and the bound session to verify readiness and turn completion. [Herdr status detection](https://herdr.dev/tui/docs/agents/#how-status-works)

The agent manager handles recoverable worker problems in the bound session. For example, it can reconnect to the same app server and verify the thread and turn before resuming observation. Escalate when recovery cannot proceed or a person needs to respond, including unanswered approvals and operator suspensions. Preserve the pane and checkout. When the worker backend confirms an interruption, deliver the interruption for the verified pane and turn. Use the native binding and turn history to identify a result if the pane has closed.

## Cron tick

Run one no-agent watchdog job per profile. Each tick follows this order:

1. Use the manager portion of `prepare-panes` to verify the workspace and recover the manager pane when necessary. Use the pane query for its effective state. End this tick unless the TUI is attached to its unlocked pane and the gateway confirms the bound session is idle with no unresolved watchdog submission.
2. Use the worker portion of `prepare-panes` to retry deferred closes, verify worker completions and escalations through the agent manager, and update worker suffixes.
3. Use `quota`. If the quota gate holds, end this tick.
4. Use the pane query to select the oldest unread worker report, either a finished result or an escalation. Submit it through the gateway and end this tick.
5. If there is no unread worker report, use `capacity` to verify that at least one configured worker is ready to start a run. End this tick if none qualifies.
6. Submit a `periodic` event through the gateway with the configured manager prompt so the workflow can discover work, then end this tick.

Worker reports take priority over the capacity check. A quota hold delays both worker reports and discovery turns. Serialize watchdog selection and admission with pane mutations for the same profile. After admitting a report, skip worker-report reads and submissions on subsequent ticks until the gateway confirms that the manager turn ended. Release the lock after admission so the manager can call engine tools during the turn.

A worker is ready to start when a slot is available, no unresolved launch conflicts with it, the agent manager verifies its backend, model and launch profile prerequisites, and its quota allows submission. This preflight uses script only. Validate the selected working directory and recheck capacity and quota at submission.

Use a stable ID for each watchdog event and submit it to the exact bound gateway conversation. Recheck the pane lock before submission. If the gateway reports that the session is busy, end the tick with the event unadmitted. If the reply is lost, look up the receipt in the gateway's conversation history using the same event ID before retrying.

## Deliver a worker result

Send a `worker_result` event with `worker`, `pane_id`, the native `turn_id` and `message`. If the full result exceeds the input limit, send a bounded excerpt with `truncated: true`. The full result is available through `query PANE_ID --turn TURN_ID`. Present the result as data alongside the configured manager prompt.

Identify a result by workspace, worker pane ID and worker turn ID. Keep one stable gateway event ID per delivery attempt. If the manager turn ends and the result is still unread, derive a new attempt ID from the last gateway receipt and try again on a later tick. If admission is uncertain, resolve that attempt before reading another result.

The engine delivers pending worker results. The workflow can act on information from any source and calls `mark-read` for the exact pane and turn after handling the result. The engine changes the suffix only if the pane still represents that turn. Accept `steer` on any eligible idle pane, regardless of prior delivery or acknowledgement. A successful `steer` supersedes the previous result for delivery and changes the pane to `working`. A refused `steer` leaves the previous state in place. The previous result remains available through native turn history.

### Worker attention

Send a `worker_attention` event with `worker`, `pane_id`, `turn_id`, `attention_id`, `state` (`blocked` or `suspended`), and the agent manager's reason. Use the same delivery and retry rules as for a worker result, with `attention_id` distinguishing separate blocks on the same turn. The workflow uses the report to surface the need for operator action and acknowledges the exact escalation with `mark-read`. The engine leaves the turn active and counts its pane against capacity after acknowledgement. When the agent manager reports that the block has cleared or the turn has finished, retire the escalation and update the pane from the verified turn state.

## Quota and capacity

Count working or possibly working panes against the worker limit, including zoom-locked panes. An unlocked idle `READ` pane leaves a slot available for another run. `steer` on an idle pane requires a free slot, regardless of its delivery state. Reserve capacity atomically with automated worker admission.

Use separate working directories for retained panes that the operator can reactivate. `run` accepts a directory chosen by the workflow and uses the configured default when omitted. Validate the resolved directory at launch and refuse a conflicting use of the same directory. The workflow prepares any project-specific checkout before invoking `run`.

On each tick, read valid rate-limit windows from the agent manager for each distinct account used by configured Codex workers. Prepare verified receivers to obtain fresh quota readings when needed. When there are no Codex workers, the Codex quota check is skipped.

The rule is fixed for each current window. The usage allowance equals the fraction of time elapsed in that window. At 40% elapsed, 40% usage is allowed and 41% usage holds. Hold when any usable window is above its allowance or a required account has no usable observation. Check the selected account again for each new Codex submission and honor provider limits. Recheck capacity on both `run` and `steer` because a periodic manager turn may issue commands after cron has finished checking it.

## Dependency skill requirements

Use the skills' public caller APIs. The engine owns pane roles, result delivery, and the rule that a zoom-locked pane counts as working.

### Herdr pane manager

The pane manager supplies workspace and pane identity, layout, and guarded mutations. [Herdr pane manager skill](https://github.com/milyin/hermes-herdr-pane-manager) · [Caller API](https://github.com/milyin/hermes-herdr-pane-manager/blob/main/references/api.md)

- Use `workspaces` and `workspace-create` in the configured local Herdr instance. Use `layout` and `lock-status` to verify workspace, pane IDs, labels, and zoom state.
- Use `create --argv` with the manager working directory and suffix to launch the Hermes TUI. Worker panes are created by the agent manager during `provision`. Retain any pane ID from a partial or uncertain creation for reconciliation.
- Use `rename` to set the application suffix while preserving the pane manager's ID prefix. Keep ordinary renames subject to the zoom check.
- Require `close` on the exact bound pane to check the target and tab zoom state at the point Herdr applies the mutation. For `user_locked` or `tab_user_locked`, the pane manager marks its displayed ID `DEAD` and returns `close_deferred` with the native pane ID. Expose pending closure in `layout`. For `lock_unknown`, hold until the lock can be verified. Use the verified layout and mutation receipts to confirm the result.

### Herdr agent manager

The agent manager supplies worker sessions, native worker turn operations, and Codex quota readings. [Herdr agent manager skill](https://github.com/milyin/hermes-herdr-agent-manager) · [Caller API](https://github.com/milyin/hermes-herdr-agent-manager/blob/main/references/api.md)

- Use `provision` with the backend and working directory, and `run --model` with the selected model. Require a scripted launch preflight that verifies the configured agent, model, working directory, and any requested launch profile without opening a worker pane or invoking a model. Pass the launch profile reference through unchanged. The agent manager applies that profile to the pane and refuses the launch if it cannot apply it. Keep credentials out of prompts and receipts.
- For Codex, `provision` must establish and report the binding between the private app server, native thread ID, and interactive worker pane. Require that the TUI can display turns submitted by the agent manager, and that the agent manager can observe turns and state changes initiated through the TUI. Hold automation while that binding cannot be verified. [Interactive Codex workers](#interactive-codex-workers)
- Require a machine-readable status query for the exact bound session, including before its first turn. Use native backend state and bound process state to distinguish idle, working, blocked, suspended and unknown. Use `wait` with the exact known turn ID to observe completion. Expose results and named run parameters by exact thread and turn through native session history, including for closed panes whose bindings can be verified. Distinguish confirmed failure or interruption from an unknown outcome.
- Recover routine worker failures through the verified session when possible. Resolve ambiguous submissions before retrying. Leave approvals and requested input to the operator. For a block that needs escalation, expose its state, reason, observed time, and a stable, label-safe `attention_id` tied to the exact pane and turn. Keep that ID through repeated observations of the same block. Report when the block clears.
- Use idle `steer` bound with `--thread-id` and `--after-turn-id`. Initial and follow-up worker submissions must enforce idle and lock checks and return a verifiable admission receipt independently of completion. Distinguish a definitive refusal from an unknown outcome.
- Use `get-remaining-tokens --agent codex` for remaining percentage, window duration, and reset time. Verify that readings and submissions refer to the same Codex account. Report unavailable or unverified observations so the project manager can hold the affected work.
