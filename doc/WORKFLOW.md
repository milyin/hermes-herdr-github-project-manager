# Hermes Herdr project manager workflow

This specification defines the target behavior. [Profile settings](PROFILE.md) define the manager and named workers. The [Hermes commands API](COMMANDS.md) exposes worker, inspection and watchdog operations.

## Responsibilities

The scripted runtime prepares panes, tracks turns, enforces quota and concurrency, and delivers worker results to the manager. The manager runs in a Hermes gateway session displayed in the Hermes terminal UI (TUI) in a dedicated Herdr pane. It decides what to do through its prompt and calls the [project-manager tools](COMMANDS.md#manager-tools) to delegate and inspect panes. The runtime uses the pane manager for layout and the agent manager for worker sessions and quota.

The installable skill is project-agnostic. User-selected manager and worker prompts own work discovery, project policy, and external service operations. The runtime handles prompt text and parameter values as opaque input. Its tools, startup integration, and watchdog schedule work from configured resources and verified pane and agent state. The [example manager prompt](MANAGER_PROMPT.md) supplies one project policy.

Provide one implementation for each operation and use it from Hermes commands, manager tools and cron. They share the same profile binding, observations, guards and result fields. In particular, manual inspection and cron use the same pane, quota, and capacity queries.

## Manager conversation

Launch `hermes -p PROFILE --tui` in the manager pane. The selected profile's Hermes gateway runs the turns and owns the conversation. The TUI lets the operator view the transcript and talk to the manager. The watchdog connects through the authenticated local gateway interface to submit scheduled instructions and worker results into that same conversation. Use the gateway interface for state and admission. [Hermes TUI gateway](https://hermes-agent.nousresearch.com/docs/user-guide/tui#how-the-tui-talks-to-its-gateway)

Bind the profile, exact Herdr pane, and gateway conversation before enabling scheduled turns. Verify that the live TUI is attached to the bound conversation and that the gateway has applied the configured working directory, model, and any launch profile to the session. Hold scheduled turns while the TUI is suspended, disconnected, or displaying another conversation. When a replacement pane is needed, reattach the TUI to the exact saved conversation. Create a fresh conversation only after the gateway confirms the previous one is gone and the runtime reconciles any outstanding submissions. Keep saved worker results available across reattachment and replacement.

### Gateway requirements

- The gateway must let the runtime verify the TUI attachment and query the exact bound session's state without starting a model turn. Apply a requested manager launch profile to the gateway session's tool execution environment. Refuse setup if the environment and credentials cannot be applied. Keep credentials scoped to the session and its tool processes.
- For watchdog messages, the gateway must check that the bound session is idle and accept its next turn in one operation. If an operator turn or a queued turn already occupies the next place, return a definitive refusal. Leave the watchdog message unqueued for a later tick.
- The gateway must record admission under a caller-supplied event ID before executing the turn. Return a receipt identifying the conversation and accepted turn. Reusing the same ID for the same message returns the original receipt. Refuse reuse with different content. Support receipt lookup after gateway restart. The runtime keeps its journal entry until it can reconcile admission.

## Manager system prompt

The project-manager plugin registers its own tools and supplies their names and argument schemas to the bound gateway session. Verify the session binding when dispatching manager tools. Register the following fixed instructions through Hermes's system-prompt section API before the manager's first turn. The editable manager prompt supplies project policy separately. [Hermes system-prompt sections](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks#cache-safe-system-prompt-sections)

> Use only the registered project-manager tools to start or steer workers, inspect panes and results, and close worker panes. Use `project_manager_run` to start a worker, `project_manager_steer` for a follow-up, `project_manager_query` for a result, and `project_manager_close` to close a pane. Leave pane preparation and scheduled delivery to the runtime.

## Interactive Codex workers

Give each Codex worker one live app-server thread. The agent manager starts the app server with the worker's launch profile, opens a thread without submitting a turn, and starts a Codex TUI in the worker pane attached to that exact thread. Use a private local connection, such as `codex --remote unix://SOCKET resume THREAD_ID` on a system with Unix socket support. The agent manager submits work and observes turn events through the same app server. Verify the attachment before sending the first task. [Codex App Server](https://developers.openai.com/codex/app-server) · [Codex TUI session client](https://github.com/openai/codex/blob/rust-v0.171.0/codex-rs/tui/src/app_server_session.rs)

The operator can read the job transcript, ask questions, give instructions, and respond to approval requests in the pane using the Codex TUI. Zoom the pane to take control. While zoomed, hold automated submissions, result delivery, and pane mutations for that worker. A running turn can continue, and the operator can steer or interrupt it through Codex. The app server applies the worker's configured execution profile to turns from both clients. Deliver approval requests to the TUI even for turns started by the agent manager, so the operator can decide.

After unlock, verify the attachment and thread state before resuming automation. If the operator has switched threads, hold until the TUI shows the original thread again. Count a turn started by either client as working. Require automated follow-ups to start only if the verified thread is still idle and its last turn matches the one observed by the caller. Refuse if operator input has changed the thread in the meantime. Preserve the exact result of each automated turn, including any instructions the operator steered into that turn. Later operator turns stay in the transcript. To send Hermes an updated result after manual work, unlock the pane and use `steer` to request a fresh report once the prior result has been delivered.

## Pane lifecycle

Use the same scripted workspace and pane verification on first launch and on each cron tick. Create the workspace through the pane manager when none is bound and record its exact ID. Bind each saved pane to its exact Herdr instance, workspace ID and pane ID. The runtime journals launches and turn submissions and saves the native thread and turn IDs and the run's named parameters. Assign a stable local turn ID when journaling submission so an interruption can be reported even if the native receipt is missing. If an operation is ambiguous, retain its intent and reconcile it before proceeding with the same pane or replacing it.

Keep the Herdr pane ID as the label prefix. The managed suffixes are:

| Suffix | Meaning |
| --- | --- |
| `pm-manager` | The Hermes TUI attached to the manager conversation. The gateway supplies the turn state. |
| `pm-quota` | A dedicated quota receiver when one is needed to read Codex account limits. |
| `pm-worker:<name>:working` | The latest turn is starting or running. |
| `pm-worker:<name>:unread` | The latest turn finished and its result has been preserved for delivery. |
| `pm-worker:<name>:read` | The result was admitted to the manager session. |
| `pm-worker:<name>:unknown` | The runtime cannot verify the pane or turn outcome. |

Verify suffix updates against the current binding. Persist results before marking a pane unread. Use the saved receipts to reconcile labels after a restart or a failed rename. Labels display state. The native session and saved turn receipts establish it.

Treat a zoom-locked pane as `working` in all scheduling and capacity decisions. A locked manager receives no scheduled turn. A locked worker counts against capacity and delivery of any unread result waits until it is unlocked. Keep the saved result and suffix while the pane is locked. After unlock, use the verified native state and reconcile the suffix. A present worker with an unknown lock still counts against capacity. Hold operations that require an unlocked or idle pane until its state is verified.

Determine manager turn state through the Hermes gateway and worker state through the agent manager. Distinguish a running turn (`working`), an agent waiting for input or approval (`blocked`), and a bound pane process stopped by the operating system (`suspended`). A suspended manager TUI prevents scheduled delivery even while the gateway keeps running. A `done` result requires a completion receipt for the exact turn. With missing or conflicting evidence, report `unknown`. Hold new submissions to blocked, suspended, and unknown sessions. An unfinished worker turn continues to occupy capacity while blocked or suspended. Use Herdr's UI agent status as evidence for a hold and the bound session to verify readiness and turn completion. [Herdr status detection](https://herdr.dev/tui/docs/agents/#how-status-works)

Preserve verified worker panes and resume observation using their saved receipts. If a Codex TUI disconnects, observe the bound app-server thread and hold submissions until the TUI reattaches. Determine the turn outcome from the app server. If a worker definitively stopped without a result, preserve its checkout and deliver an interruption result under its saved pane ID. That result remains eligible for delivery even if no live pane remains to rename. Treat a worker that may still be active as unresolved. Recovery must preserve the record of any submitted work and avoid replaying it.

## Cron tick

Run one no-agent watchdog job per profile. Each tick follows this order:

1. Use `prepare-panes` to verify the workspace and managed panes. Start or recover required panes and observe worker completions using the saved bindings. Preserve results and update worker suffixes. Stop on an unresolved manager binding.
2. Use `quota`. If the quota gate holds, end this tick.
3. Use the pane query for the manager's effective state. End this tick unless the TUI is attached to the bound conversation, its pane is unlocked, and the gateway reports that the session is idle.
4. Use the pane query for finished unread worker turns and select the oldest. Submit its saved result through the gateway and mark that exact turn read after verified admission. End this tick.
5. If there is no unread result, use `capacity` to verify that at least one configured worker is ready to start a run. End this tick if none qualifies.
6. Submit a `periodic` event through the gateway with the configured manager prompt so it can discover work, then end this tick.

A result takes priority over the capacity check. A quota hold delays both types of manager turn. Serialize overlapping ticks and reserve capacity atomically with worker start or follow-up. Reserve the resolved working directory for its pane while the pane is open so another run cannot reuse the same checkout while a follow-up is possible. Release the synchronization lock after admission and bookkeeping so the manager can call worker commands from its turn.

A worker is ready to start when a slot and its working directory are available, no unresolved launch conflicts with it, the agent manager verifies its backend, model and launch profile prerequisites, and its quota allows submission. This preflight uses script only. The manager discovers eligible work during its turn. Recheck the selected worker at actual submission.

Journal each watchdog event, including `periodic`, with its target conversation and stable ID before asking the gateway to accept it. Recheck the pane lock before submission. If the gateway reports that the session is busy, end the tick with the event unadmitted. If the reply is lost, look up the receipt under the same event ID before making another submission.

## Deliver a worker result

Send a `worker_result` event with `worker`, `pane_id`, `turn_id` and `message`. Use the journaled worker turn ID. If the full result exceeds the input limit, send a bounded excerpt with `truncated: true`. The full saved result is available through `query PANE_ID --turn TURN_ID`. Present the result as data alongside the configured manager instructions.

Key the worker result event by workspace, worker pane ID and worker turn ID. On confirmed gateway admission, persist the receipt and rename the worker pane to `read` only if it still represents that turn. After an interruption, look up the exact event's receipt and use it to finish the label update. If admission cannot be determined, hold that delivery for reconciliation. A manager command acting on the result must see the committed delivery receipt before it can start another turn or close that pane.

## Quota and capacity

On each tick, read valid rate-limit windows from the agent manager for each distinct account used by configured Codex workers. Prepare verified receivers to obtain fresh quota readings when needed. When there are no Codex workers, the Codex quota check is skipped.

The rule is fixed for each current window. The usage allowance equals the fraction of time elapsed in that window. At 40% elapsed, 40% usage is allowed and 41% usage holds. Hold when any usable window is above its allowance or a required account has no usable observation. Check the selected account again for each new Codex submission and honor provider limits. Recheck capacity on both `run` and `steer` because a periodic manager turn may issue commands after cron has finished checking it.

## Dependency skill requirements

Use the skills' public caller APIs. The project manager owns pane roles, result delivery, and the rule that a zoom-locked pane counts as working.

### Herdr pane manager

The pane manager supplies workspace and pane identity, layout, and guarded mutations. [Herdr pane manager skill](https://github.com/milyin/hermes-herdr-pane-manager) · [Caller API](https://github.com/milyin/hermes-herdr-pane-manager/blob/main/references/api.md)

- Use `workspaces` and `workspace-create` in the configured local Herdr instance. Use `layout` and `lock-status` to verify workspace, pane IDs, labels, and zoom state.
- Use `create --argv` with the manager working directory and suffix to launch the Hermes TUI. Worker panes are created by the agent manager during `provision`. Retain any pane ID from a partial or uncertain creation for reconciliation.
- Use `rename` to set a suffix while preserving the pane ID prefix. Use `close` on the exact bound pane. Respect `user_locked` and `lock_unknown`, and `tab_user_locked` when closing a sibling in a zoomed tab. Use the verified layout and mutation receipts to confirm the result.

### Herdr agent manager

The agent manager supplies worker sessions, native worker turn operations, and Codex quota readings. [Herdr agent manager skill](https://github.com/milyin/hermes-herdr-agent-manager) · [Caller API](https://github.com/milyin/hermes-herdr-agent-manager/blob/main/references/api.md)

- Use `provision` with the backend and working directory, and `run --model` with the selected model. Require a scripted launch preflight that verifies the configured agent, model, working directory, and any requested launch profile without opening a worker pane or invoking a model. Pass the launch profile reference through unchanged. The agent manager applies that profile to the pane and refuses the launch if it cannot apply it. Keep credentials out of prompts and receipts.
- For Codex, `provision` must establish and report the binding between the private app server, native thread ID, and interactive worker pane. Require that the TUI can display turns submitted by the agent manager, and that the agent manager can observe turns and state changes initiated through the TUI. Hold automation while that binding cannot be verified. [Interactive Codex workers](#interactive-codex-workers)
- Require a machine-readable status query for the exact bound session, including before its first turn. Use native backend state and bound process state to distinguish idle, working, blocked, suspended and unknown. Use `wait` with the exact known turn ID to observe completion. Preserve a result correlated to the exact thread and turn before another turn can replace it. Distinguish confirmed failure or interruption from an unknown outcome.
- Use idle `steer` bound with `--thread-id` and `--after-turn-id`. Initial and follow-up worker submissions must enforce idle and lock checks and return a verifiable admission receipt independently of completion. Distinguish a definitive refusal from an unknown outcome.
- Use `get-remaining-tokens --agent codex` for remaining percentage, window duration, and reset time. Verify that readings and submissions refer to the same Codex account. Report unavailable or unverified observations so the project manager can hold the affected work.
