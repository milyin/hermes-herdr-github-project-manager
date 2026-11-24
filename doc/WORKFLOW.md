# Hermes Herdr GitHub Project Manager workflow specification

**Draft for human review.** This is the working design document for the project manager. Once agreed, it is the single source for the implementation, Hermes skill instructions and prompts, configuration, operator documentation, dashboard, and tests. Derive those materials from the accepted workflow. Keep changes to behavior traceable to decisions here.

The dependency APIs define the operations they expose. This specification defines when the project manager may call them and what evidence it must retain. [Current implementation and migration](#current-implementation-and-migration) records where the existing code differs from the proposed design.

## Roles and scope

| Actor | Responsibility |
| --- | --- |
| Operator or authorized triager | Configures the profile, workspace, repository access, identities and policy. Routes issues and resolves ambiguous operations. The operator controls activation and resolves holds requiring human action. |
| Hermes project manager | Owns issue intake, worker selection, assignment policy, quota decisions, handoffs, and project status. One interactive Hermes controller and its in-pane supervisor share a single serialized workflow per profile. |
| Herdr agent manager | Provides verified receiver provisioning, account quota observations, prompt submission, bound follow-ups, and exact turn results through its public API. |
| Herdr pane manager | Provides scoped layout and guarded pane lifecycle operations. Pane identity is the full tuple of Herdr instance, workspace ID, and pane ID. User locks and ambiguous lock status hold mutations. |
| Worker | Owns the assigned job in its own checkout, including pulling the PR, implementing and testing changes, pushing results, and making any required review requests. Uses the assigned GitHub App account and reports the outcome and any action a human needs to take. |
| Scheduler watchdog | Checks the controller's identity and liveness, and starts it in the configured workspace when recovery conditions permit. The in-pane supervisor owns periodic project work. |

## Profile, startup and periodic passes

1. Opening the configured project-manager profile automatically starts [profile setup](PROFILE.md#first-run). Serialize setup per profile. Install or verify all three skills and read project settings from that profile. Reuse the verified manager workspace or create it in the configured local Herdr session through the pane manager. Persist the exact workspace ID. Standalone commands remain available for diagnostics and recovery.
2. Run `check` to verify the scoped layout, GitHub identities, canonical repository IDs, names and HTTPS clone endpoints, and readable initialized wiki HEADs for the dashboard and owned repositories. Run `preview` for a private dry run. It may read issue routes, layout, quota and versions and save a local report. GitHub writes, worker starts, cleanup and skill updates require apply mode.
3. For Codex workers, run `quota-setup` when a compatible receiver has not already been bound. Verify that its observations apply to the worker account and that the configured quota policy can be evaluated. Run `activate` after preflight. Activation binds the effective profile settings. Changing them requires verification and reactivation. A known quota hold can leave an activated controller observing existing work while new turns wait.
4. Prepare and enable one Hermes cron watchdog job with `--no-agent` in the selected profile. Reuse or start a Hermes scheduler that serves the profile and will keep running after the opening session exits. Reuse a matching job on later launches and keep overlapping managers paused. Trigger the registered job immediately on first setup so the manager starts without waiting for the scheduled interval. See [Hermes cron operations](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron#lifecycle-actions).
5. The watchdog runs the guarded apply startup. It journals creation, uses the pane manager, verifies the controller in the exact pane, and confirms its heartbeat. Retire a preview controller through guarded pane operations before replacing it with an apply controller. Report first run as ready once the manager has a fresh apply heartbeat and the enabled watchdog has a successful recorded run. Verify the latest in-pane pass separately with `show`. The watchdog checks liveness and starts the manager when needed. The in-pane supervisor owns periodic project work.
6. In each apply pass, read GitHub routes and host state, reconcile saved work, admit a bounded amount of new work, and publish eligible status. Serialize passes and direct mutations with profile locks. `follow-up` and `capability-gap` require the same settings and a verified live apply supervisor on the clean installed revision. Recheck before each effect. A pending update or restart holds these mutations.

## Route GitHub issues

- Accept open issues carrying exactly one manager routing label, `hermes:<name>`. An authorized triager applies it. An apply controller can create the dashboard's routing and target labels.
- For an owned repository's issue, use that repository as the target. For a dashboard issue, require both its `[owner/repository] ` title prefix and the matching target label returned by `routes`. The pair must resolve to one allowlisted repository. Use the dashboard queue for external repositories. Give the assigned app account the GitHub access needed for the job.
- Save the immutable source repository ID, issue ID and target repository ID with the assignment. The human-readable selector is `OWNER/REPO#N`. Revalidate the binding and current authorization before assigning, following up, or posting dashboard status. A transfer, replaced repository, conflicting route, or old row without a verifiable binding goes to human attention. Coordinate route changes while a worker is in flight. Relabeling blocks future submissions under the old route and does not terminate the accepted turn.
- Keep one live worker assignment per bound issue. Repeated discovery or a restart must reuse its existing evidence. Process eligible issues in stable issue-key order within the configured per-pass and worker capacity limits. Count starting, active and unresolved worker submissions against capacity.

## Decide quota and assign work

The project manager owns the quota policy and the decision to submit each new Codex turn, including idle follow-ups. Request fresh account windows through the agent manager's public quota observation interface. The installed raw operation is `get-remaining-tokens --agent codex`, which reports remaining percentage, window duration and reset time. Accept observations only for the account and limit bucket used by the intended receiver.

The default project policy uses each valid, unexpired window. A window is usable when its reported remaining percentage is finite and between 0 and 100, its duration is positive, and its future reset is within that duration. Allow a turn only if at least one window is usable and every usable window has a positive remaining percentage at least equal to the percentage of the window's time remaining. Any profile-specific quota rule must be defined in this specification before the configuration schema exposes it. Legacy quota fields remain inert during migration. A missing required window, stale or unverified observation, exhausted window, or window behind policy leaves the task waiting with a reason. Re-evaluate on a later pass. The project manager makes this decision near submission and discards it when the observation expires or the account or assignment changes. A decision covers one submission and provides no cross-pane or cross-profile token reservation.

The agent manager supplies observations and enforces transport, identity, lock and turn-binding preconditions. In the target workflow it accepts the project manager's decision for that exact submission and does not apply its own workload pacing policy. The worker receives the task after admission. Provider refusals remain authoritative. Until the dependency has a public submission contract supporting this split, use the current quota-aware path described below and never use `--override-quota` as a migration mechanism.

1. Check activation, route, repository identity, host CPU and disk, capacity, and quota before provisioning. Prepare a separate working directory for the assignment and save its identity, the target base, and a bounded issue snapshot. Enable the network access needed for the worker to pull and push on GitHub. The prompt specifies the job scope, the PR or target base, the assigned GitHub App account, acceptance criteria, and the expected response. The worker handles review requests within the job through authorized channels. If a request needs a human action or access the worker lacks, the worker reports the action in its response. Pass issue content as data and pass prompts on stdin.
2. Persist the intent before provisioning. Use the agent manager to provision the receiver with `worker.agent`. Verify the reported backend and checkout, and confirm the returned pane is new in the exact configured layout. Save that receipt before sending work.
3. Recheck the live route, issue snapshot, pane, host health, configuration and any applicable quota. Persist intent before submission. Use the agent manager to submit the prompt with the same backend. Save the exact pane, thread, turn, and model from the submission receipt. A verified receipt for that backend enters `running`.
4. Wait for the response through the agent manager using the saved pane and turn ID. A pending turn stays running. Accept a response only when its thread and turn IDs match. Save the full response privately and update the task status from its reported outcome. Use an idle follow-up only on the saved thread and prior turn with both `--thread-id` and `--after-turn-id`. A bound pending follow-up can be polled once its new receipt is known.
5. A confirmed refusal without submission retains the checkout and any provisioned receiver for reconciliation. Reuse that receiver only after verifying its binding and idle state. An ambiguous effect enters `uncertain`. Preserve the original journal and do not retry, provision a replacement, or send an unbound prompt. Quota or health holds gate new turns while accepted turns remain in place.

## Worker response and dashboard status

The worker reports `success` if it completed the assigned work or `failure` if it could not complete it. The response includes a brief summary, relevant evidence, and whether a human needs to act. When human action is needed, the response specifies what the human should do.

The project manager records the outcome on the dashboard. Put the task in **Need attention** only when it requires human intervention. Show **Success** and the requested action when the worker completed its part and needs a human to take the next step. Show **Failure** and the requested action when the worker could not complete its part and a human must resolve a blocker. Keep other completed outcomes in the task's status history. If a verified turn completes without a usable response, record a failed handoff and ask for human attention if it cannot be resolved through the saved turn. A pending turn remains in progress.

## Worker GitHub access

The worker performs the GitHub steps for the assignment. It pulls the existing PR or checks out the assigned base, pushes its changes to the PR branch, and creates or updates the PR as needed. It makes any review requests required by the job. Its response includes the PR link, pushed commit SHA, test results, and any human action still needed.

The job prompt instructs the worker to leave the merge into the destination branch to a human. The project manager imposes no technical merge restriction. The worker's GitHub operations are subject to the permissions of its assigned app account.

The prompt requires the worker to use only the GitHub App set in `worker.github_identity` for GitHub access. In this deployment that account is `milyin-hermes` (`milyin-hermes[bot]` on GitHub). The worker uses the configured app access for Git and GitHub operations and reports an access blocker if it cannot proceed under that identity. Account separation is a prompt-level rule until the agent manager can enforce it. [Agent manager issue #6](https://github.com/milyin/hermes-herdr-agent-manager/issues/6)

## Recovery and status

| State | Required evidence and next action |
| --- | --- |
| Queued | Current authorized route and no live assignment. Dispatch when all gates allow it. |
| Starting | Durable intent preceding worker provisioning or prompt submission. An interruption before a verified receipt requires reconciliation. |
| Running | Exact backend submission receipt. Wait on that turn only. |
| Waiting | Known quota or dispatch hold, or a pending follow-up that requires no human action. Preserve any checkout and receiver binding. |
| Uncertain | Side effect may have happened or its evidence has become unavailable. Preserve state and reconcile before any further mutations of that assignment. |
| Need attention | Human intervention is required. Display the reported Success or Failure and the requested action. For a manager-side incident, show the reason and the needed action, with any unverified worker outcome marked as such. |
| Finished | Success or Failure as reported by the worker, with no human action pending. Preserve recorded evidence under the configured retention policy. Record GitHub closure or merge separately when observed. |

After a gateway or manager restart, recover from the saved configuration, scoped pane receipt and state. A stopped but preserved controller pane, changed workspace or unresolved creation requires operator reconciliation. Never adopt an arbitrary pane or spawn over unknown work. Use `retire-uncertain` only after a verified layout proves the saved worker pane has disappeared and the operator has inspected the saved checkout. This releases capacity without replay.

The dashboard wiki contains manager-owned pages for work queues, issue and PR links, verified pane layout, health and a sanitized operation log. Owned repositories get their own manager page. Write only manager-owned pages with fast-forward Git updates and verify the remote result. Keep local paths, raw reports, terminal output, thread IDs and precise quota in private state. Keep private or internal repository facts out of other repositories' wikis. Only remove expired completed reports and unused temporary clones in manager-owned scratch space. Preserve active evidence, assigned checkouts and the last readable state if a write or capacity limit is reached.

## Skill changes and updates

Make changes to each skill through a reviewed PR in its own repository. Use `capability-gap SKILL short-slug` for a verified dependency gap, then assign the job through the ordinary worker workflow. Include any required review requests in that assignment. Preserve a blocked candidate and its evidence. Merged source and activation are separate milestones.

Read the canonical `main` refs at startup and at the next pass after `schedule.skill_update_interval_seconds` elapses, default 1800. `versions` is a read-only probe. In apply mode, attempt updates in dependency order, pane manager, agent manager, then project manager. Use only a clean same-profile `main` checkout at a quiet checkpoint without unfinished turns or candidates. Require readable unlocked Curator state, verified origin and local execution files, and trusted push CI for the exact remote head. Apply the same checks to one-shot `sync SKILL --approved-head SHA` with the supervisor stopped.

Every new first-parent commit in the update range must correspond to a PR merged into that repository's `main`, with the reviewed head's tree and the same reviewed base. Require a single `Hermes-Update-Base: BASE_SHA` line in the reviewed head commit and verified independent approval of that head and range before merge. Hold on direct pushes, insufficient evidence, locked or dirty installs, or potential file collisions. Fast-forward only. Self-update requires current dependencies. Preserve the Hermes pane and hold dispatch until a verified restart loads the new revision and reconciles any `skill-update.pending.json` receipt.

## Current implementation and migration

This section records compatibility with the implementation at the base of draft PR #4. It does not define a second workflow.

| Area | Current implementation | Work required for conformance |
| --- | --- | --- |
| Profile setup | The manager reads a separate project file and expects installation, activation and scheduler registration as individual steps. | Read project settings from the selected Hermes profile. When Hermes opens that profile, perform preflight and activation, enable the watchdog, and use its first run to start the manager workspace. |
| Quota | Passes use the agent manager's interpreted `check-quota` result. The receiver independently applies pacing before initial Codex runs and idle follow-ups. The project manager accepts legacy `quota` fields without using them. | Specify and implement an agent-manager submission API that takes an exact, fresh project-manager admission decision and preserves receiver protections. Confirm the quota observation can be bound to the intended worker account and limit bucket. Implement project policy and migrate profile configuration deliberately. Keep both existing checks during transition. |
| Follow-ups and recovery | Follow-ups use bound IDs, but the project manager only accepts an inline completed result. Its state can become uncertain when provisioning succeeds but later submission is refused. | Track pending follow-ups by their verified native receipt. Distinguish a confirmed refusal from an ambiguous send and safely continue with a verified idle receiver. |
| Worker GitHub operations | The current manager stages a candidate bundle and owns a signed PR publication path. The worker defaults to offline. | Give the assigned worker GitHub access for the job. Move pulling, pushing, and PR updates into the worker assignment and remove the manager-mediated publication path. Apply the prompt-level identity rule until the agent manager supports technical enforcement. |
| Worker outcome | The manager saves the worker's response, then relies on PR status, CI and externally supplied approval for its readiness state. | Update task status from the worker's response. Route Success or Failure to **Need attention** when the response calls for human intervention. Remove task-level review coordination and readiness gates from the manager. |

Verify conformance through the ordinary entry points. Cover preview without external writes or agent starts, apply with and without a live bound controller, conflicting and stale routes, quota and host refusal before submission, duplicate passes, ambiguous effects across restart, and both follow-up bindings. Exercise successful and failed worker responses with and without human action, malformed responses, wiki disclosure and update holds. Verify that the prompt names the assigned GitHub App and that the worker can pull, push, and update the PR. Verify account isolation once the agent manager supplies it. Distinguish unit and simulated tests, hosted CI, and live same-profile verification.

Verify first run by opening a configured profile and sending no message. Check the new workspace, live apply manager pane and successful run of the enabled watchdog. Reopen the profile and verify that it reuses them. After closing the opening session, verify that the scheduler still runs the watchdog.
