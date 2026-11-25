# Hermes Herdr GitHub project manager workflow

This specification defines the target behavior. [Profile settings](PROFILE.md) define the manager and named workers. The [Hermes commands API](COMMANDS.md) exposes worker, inspection and watchdog operations. The [manager prompt](MANAGER_PROMPT.md) defines the GitHub workflow.

## Responsibilities

The scripted runtime prepares panes, tracks turns, enforces quota and concurrency, and delivers worker results to the manager. The manager is a Hermes session in a dedicated Herdr pane. It decides what to do through its prompt and calls the [project-manager tools](COMMANDS.md#manager-tools) to delegate and inspect panes. The runtime uses the [dependency skills](#dependency-skill-requirements) for the underlying agent and pane operations.

Provide one implementation for each operation and use it from Hermes commands, manager tools and cron. They share the same profile binding, observations, guards and result fields. In particular, manual inspection and cron use the same pane, quota, and capacity queries.

Use the manager session's verified submission channel for scheduled instructions and results. It must report manager turn state and admit a turn only when idle. A pane write or a label change is insufficient proof of prompt admission. Deliver each result in the manager pane's Hermes conversation so the manager can act on it in that turn. The runtime watches workers and sends their results proactively.

## Manager system prompt

The project-manager plugin registers its own tools and supplies their names and argument schemas to the manager session. Register the following fixed instructions through Hermes's system-prompt section API for the bound manager session before its first turn. The editable manager prompt supplies project policy separately. [Hermes system-prompt sections](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks#cache-safe-system-prompt-sections)

> Use only the registered project-manager tools to start or steer workers, inspect panes and results, and close worker panes. Use `project_manager_run` to start a worker, `project_manager_steer` for a follow-up, `project_manager_query` for a result, and `project_manager_close` to close a pane. Leave pane preparation and scheduled delivery to the runtime.

## Pane lifecycle

Use the same scripted workspace and pane verification on first launch and on each cron tick. Create the workspace through the pane manager when none is bound and record its exact ID. Bind each saved pane to its exact Herdr instance, workspace ID and pane ID. The runtime journals launches and turn submissions and saves the native thread and turn IDs and the run's named parameters. Assign a stable local turn ID when journaling submission so an interruption can be reported even if the native receipt is missing. If an operation is ambiguous, retain its intent and reconcile it before proceeding with the same pane or replacing it.

Keep the Herdr pane ID as the label prefix. The managed suffixes are:

| Suffix | Meaning |
| --- | --- |
| `pm-manager` | The bound manager session. The backend receipt identifies whether its turn is idle or busy. |
| `pm-quota` | A dedicated quota receiver when one is needed to read Codex account limits. |
| `pm-worker:<name>:working` | The latest turn is starting or running. |
| `pm-worker:<name>:unread` | The latest turn finished and its result has been preserved for delivery. |
| `pm-worker:<name>:read` | The result was admitted to the manager session. |
| `pm-worker:<name>:unknown` | The runtime cannot verify the pane or turn outcome. |

Verify suffix updates against the current binding. Persist results before marking a pane unread. Use the saved receipts to reconcile labels after a restart or a failed rename. Labels display state. The native session and saved turn receipts establish it.

Treat a zoom-locked pane as `working` in all scheduling and capacity decisions. A locked manager receives no scheduled turn. A locked worker counts against capacity and delivery of any unread result waits until it is unlocked. Keep the saved result and suffix while the pane is locked. After unlock, use the verified native state and reconcile the suffix. A present worker with an unknown lock still counts against capacity. Hold operations that require an unlocked or idle pane until its state is verified.

Start or recover the manager pane if it is absent and safe to recreate. If its previous session is confirmed lost, start a fresh Hermes manager with the same profile and prompt. Preserve verified worker panes and resume observation using their saved receipts. If a worker definitively stopped without a result, preserve its checkout and deliver an interruption result under its saved pane ID. That result remains eligible for delivery even if no live pane remains to rename. Treat a worker that may still be active as unresolved. Recovery must preserve the record of any submitted work and avoid replaying it.

## Cron tick

Run one no-agent watchdog job per profile. Each tick follows this order:

1. Use `prepare-panes` to verify the workspace and managed panes. Start or recover required panes and observe worker completions using the saved bindings. Preserve results and update worker suffixes. Stop on an unresolved manager binding.
2. Use `quota`. If the quota gate holds, end this tick.
3. Use the pane query for the manager's effective state. If it is working or its idle state cannot be verified, end this tick.
4. Use the pane query for finished unread worker turns and select the oldest. Submit its saved result to the manager and mark that exact turn read after verified admission. End this tick.
5. If there is no unread result, use `capacity`. If the maximum number of working panes is reached, end this tick.
6. Submit a `periodic` event with the configured manager prompt, then end this tick.

A result takes priority over the capacity check. A quota hold delays both types of manager turn. Serialize overlapping ticks and reserve capacity atomically with worker start or follow-up. Reserve the resolved working directory for its pane while the pane is open so another run cannot reuse the same checkout while a follow-up is possible. Release the synchronization lock after admission and bookkeeping so the manager can call worker commands from its turn.

## Deliver a worker result

Send a `worker_result` event with `worker`, `pane_id`, `turn_id` and `message`. Use the journaled worker turn ID. If the full result exceeds the input limit, send a bounded excerpt with `truncated: true`. The full saved result is available through `query PANE_ID --turn TURN_ID`. Present the result as data alongside the configured manager instructions.

Key the delivery by workspace, worker pane ID and worker turn ID. Journal the delivery before submitting. On confirmed manager admission, persist the admission receipt and rename the worker pane to `read` only if it still represents that turn. After an interruption, reconcile the exact delivery and use its admission receipt to finish the label update. If admission cannot be determined, hold that delivery for reconciliation. Neither a later tick nor a manager restart may blindly resend it. A manager command acting on the result must see the committed delivery receipt before it can start another turn or close that pane.

## Quota and capacity

On each tick, read valid rate-limit windows from the agent manager for each distinct account used by configured Codex workers. Prepare verified receivers to obtain fresh quota readings when needed. When there are no Codex workers, the Codex quota check is skipped.

The rule is fixed for each current window. The usage allowance equals the fraction of time elapsed in that window. At 40% elapsed, 40% usage is allowed and 41% usage holds. Hold when any usable window is above its allowance or a required account has no usable observation. Check the selected account again for each new Codex submission and honor provider limits. Recheck capacity on both `run` and `steer` because a periodic manager turn may issue commands after cron has finished checking it.

## Dependency skill requirements

Use the skills' public caller APIs. The project manager owns pane roles, result delivery, and the rule that a zoom-locked pane counts as working.

### Herdr pane manager

The pane manager supplies workspace and pane identity, layout, and guarded mutations. [Herdr pane manager skill](https://github.com/milyin/hermes-herdr-pane-manager) · [Caller API](https://github.com/milyin/hermes-herdr-pane-manager/blob/main/references/api.md)

- Use `workspaces` and `workspace-create` in the configured local Herdr instance. Use `layout` and `lock-status` to verify workspace, pane IDs, labels, and zoom state.
- Use `create --argv` with the working directory and suffix to launch the pane. Retain any pane ID from a partial or uncertain creation for reconciliation.
- Use `rename` to set a suffix while preserving the pane ID prefix. Use `close` on the exact bound pane. Respect `user_locked` and `lock_unknown`, and `tab_user_locked` when closing a sibling in a zoomed tab. Use the verified layout and mutation receipts to confirm the result.

### Herdr agent manager

The agent manager supplies Codex and Hermes sessions, native turn operations, and Codex quota readings. [Herdr agent manager skill](https://github.com/milyin/hermes-herdr-agent-manager) · [Caller API](https://github.com/milyin/hermes-herdr-agent-manager/blob/main/references/api.md)

- Use `provision` with the backend and working directory, and `run --model` with the selected model. Require per-pane Git and GitHub authentication bound to the requested App identity. Refuse when the identity cannot be supplied. Keep credentials out of prompts and receipts.
- Use `wait` with the exact known turn ID to observe completion. Require a way to verify the bound session's idle or busy state even before its first turn. Preserve a result correlated to the exact thread and turn before another turn can replace it. Distinguish confirmed failure or interruption from an unknown outcome.
- Use idle `steer` bound with `--thread-id` and `--after-turn-id`. Initial and follow-up submissions must enforce idle and lock checks and return a verifiable admission receipt independently of completion. A receipt for a Hermes manager turn must identify the admission into the bound conversation where the result or periodic prompt is delivered. Distinguish a definitive refusal from an unknown outcome.
- Use `get-remaining-tokens --agent codex` for remaining percentage, window duration, and reset time. Verify that readings and submissions refer to the same Codex account. Report unavailable or unverified observations so the project manager can hold the affected work.
