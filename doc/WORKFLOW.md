# Hermes Herdr GitHub project manager workflow

This specification defines the target behavior. [Profile settings](PROFILE.md) define the manager and named workers. The [Hermes commands API](COMMANDS.md) exposes worker, inspection and watchdog operations. The [manager prompt](MANAGER_PROMPT.md) defines the GitHub workflow.

## Responsibilities

The scripted runtime prepares panes, tracks turns, enforces quota and concurrency, and delivers worker results to the manager. The manager is a Hermes session in a dedicated Herdr pane. It receives the configured instructions and worker results in that session. It decides what to do through its prompt and uses the worker API to delegate. Use the [dependency skills](#dependency-skill-requirements) for agent sessions and pane operations.

Provide one implementation for each operation and use it from Hermes commands, manager tools and cron. They share the same profile binding, observations, guards and result fields. In particular, manual inspection and cron use the same pane, quota, and capacity queries.

Use the manager session's verified submission channel for scheduled instructions and results. It must report manager turn state and admit a turn only when idle. A pane write or a label change is insufficient proof of prompt admission. Deliver each result in the manager pane's Hermes conversation so the manager can act on it in that turn. The runtime watches workers and sends their results proactively.

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

These contracts define what the project manager requires of both skills. Changes needed to provide them belong in the respective dependency.

### Herdr pane manager

- Discover and create a workspace in an exact local Herdr instance. Return server-issued workspace and pane IDs. Provide a scoped layout with pane IDs, labels and verified zoom-lock state.
- Create a pane with a literal argument vector, working directory and suffix. Return the new pane's identity and distinguish a refused creation from an uncertain one.
- Rename and close only the exact pane in the specified workspace. Preserve the pane ID prefix on rename. Check current pane and tab locks before mutation and refuse when the applicable lock is set or unverified. Return verifiable results for rename and closure.

### Herdr agent manager

- Provision the requested agent backend and model in the configured working directory, using the pane manager for pane operations. Bind Git and GitHub authentication to the requested App identity. Refuse when that identity cannot be supplied. Keep credentials out of prompts and receipts.
- Observe the bound agent's native state and retrieve a terminal result for the exact thread and turn. Distinguish a running turn, confirmed completion or interruption, and an unknown outcome. Support observation without starting a turn.
- Submit an initial prompt to the verified pane. Bind follow-ups to that pane's saved thread and prior turn. Reject a busy or locked target, and return a verified submission receipt without waiting for completion. Distinguish a definitive refusal from an unknown outcome. Provide the same verified idle-only admission for the manager's Hermes session.
- Read fresh Codex quota windows associated with the account used by that worker, including remaining usage, window duration and reset time. Report when the account or readings cannot be verified. The project manager uses those readings for pacing.
