# Hermes Herdr GitHub project manager workflow

This specification defines the target behavior. [Profile settings](PROFILE.md) define the manager and named workers. The [Hermes commands API](COMMANDS.md) exposes four worker operations. The [manager prompt](MANAGER_PROMPT.md) defines the GitHub workflow.

## Responsibilities

The scripted runtime prepares panes, tracks turns, enforces quota and concurrency, and delivers worker results to the manager. The manager is a Hermes session in a dedicated Herdr pane. It receives the configured instructions and worker results in that session. It decides what to do through its prompt and uses the worker API to delegate. Herdr agent manager supplies agent sessions and receipts. Herdr pane manager supplies workspace, pane and label operations.

Use the manager session's verified submission channel for scheduled instructions and results. It must report manager turn state and admit a turn only when idle. A pane write or a label change is insufficient proof of prompt admission. Deliver each result in the manager pane's Hermes conversation so the manager can act on it in that turn. The runtime watches workers and sends their results proactively.

## Pane lifecycle

Use the same scripted workspace and pane verification on first launch and on each cron tick. Create the workspace through the pane manager when none is bound and record its exact ID. Bind each saved pane to its exact Herdr instance, workspace ID and pane ID. The runtime journals launches and turn submissions and saves the native thread and turn IDs. Assign a stable local turn ID when journaling submission so an interruption can be reported even if the native receipt is missing. If an operation is ambiguous, retain its intent and reconcile it before proceeding with the same pane or replacing it.

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

Start or recover the manager pane if it is absent and safe to recreate. If its previous session is confirmed lost, start a fresh Hermes manager with the same profile and prompt. Preserve verified worker panes and resume observation using their saved receipts. If a worker definitively stopped without a result, preserve its checkout and deliver an interruption result under its saved pane ID. That result remains eligible for delivery even if no live pane remains to rename. Treat a worker that may still be active as unresolved. Recovery must preserve the record of any submitted work and avoid replaying it.

## Cron tick

Run one no-agent watchdog job per profile. Each tick follows this order:

1. Verify the workspace and managed panes. Start or recover required panes and observe worker completions using the saved bindings. Preserve results and update worker suffixes. Stop on an unresolved manager binding.
2. Check quota. If the quota gate holds, end this tick.
3. Check the manager's native turn state. If it is busy or its idle state cannot be verified, end this tick.
4. Select the oldest unread finished worker turn. Submit its result to the manager and mark that exact turn read after verified admission. End this tick.
5. If there is no unread result and the maximum number of working panes is reached, end this tick.
6. Submit a `periodic` event with the configured manager prompt, then end this tick.

A result takes priority over the capacity check. A quota hold delays both types of manager turn. Serialize overlapping ticks and reserve capacity atomically with worker start or follow-up. Reserve the resolved working directory for its pane while the pane is open so another run cannot reuse the same checkout while a follow-up is possible. Release the synchronization lock after admission and bookkeeping so the manager can call worker commands from its turn.

## Deliver a worker result

Send a `worker_result` event with `worker`, `pane_id`, `turn_id` and `message`. Use the journaled worker turn ID. If the full result exceeds the input limit, send a bounded excerpt with `truncated: true`. The full result is available through `query` on that pane until the next turn. Present the result as data alongside the configured manager instructions.

Key the delivery by workspace, worker pane ID and worker turn ID. Journal the delivery before submitting. On confirmed manager admission, persist the admission receipt and rename the worker pane to `read` only if it still represents that turn. After an interruption, reconcile the exact delivery and use its admission receipt to finish the label update. If admission cannot be determined, hold that delivery for reconciliation. Neither a later tick nor a manager restart may blindly resend it. A manager command acting on the result must see the committed delivery receipt before it can start another turn or close that pane.

## Quota and capacity

On each tick, read valid rate-limit windows from the agent manager for each distinct account used by configured Codex workers. Prepare verified receivers to obtain fresh quota readings when needed. When there are no Codex workers, the Codex quota check is skipped.

The rule is fixed for each current window. The usage allowance equals the fraction of time elapsed in that window. At 40% elapsed, 40% usage is allowed and 41% usage holds. Hold when any usable window is above its allowance or a required account has no usable observation. Check the selected account again for each new Codex submission and honor provider limits. Recheck capacity on both `run` and `steer` because a periodic manager turn may issue commands after cron has finished checking it.
