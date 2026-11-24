# Hermes Herdr GitHub Project Manager workflow specification

**Draft for human review.** This is the working design document for the project manager. Once agreed, it is the single source for the implementation, Hermes skill instructions and prompts, configuration, operator documentation, dashboard, and tests. Derive those materials from the accepted workflow. Keep changes to behavior traceable to decisions here.

The dependency APIs define the operations they expose. This specification defines when the project manager may call them and what evidence it must retain.

## Roles and scope

| Actor | Responsibility |
| --- | --- |
| Operator or authorized triager | Configures the profile, workspace, repository access, identities and policy. Assigns issues and resolves holds requiring human action. |
| Hermes project manager | Owns issue intake, worker selection, assignment policy, quota decisions, handoffs, and project status. One interactive Hermes controller and its in-pane supervisor share a single serialized workflow per profile. |
| Herdr agent manager | Provides verified receiver provisioning, account quota observations, prompt submission, bound follow-ups, and exact turn results through its public API. |
| Herdr pane manager | Provides scoped layout and guarded pane lifecycle operations. Pane identity is the full tuple of Herdr instance, workspace ID, and pane ID. User locks and ambiguous lock status hold mutations. |
| Worker | Owns the assigned job in its own checkout, including pulling the PR, implementing and testing changes, pushing results, and making any required review requests. Uses the assigned GitHub App account and reports the outcome and any action a human needs to take. |
| Scheduler watchdog | Runs pane preparation to verify the manager's workspace and panes and restore required panes when recovery conditions permit. The in-pane supervisor owns periodic project work. |

## Profile, startup and periodic passes

1. Opening an enabled project-manager profile automatically starts [profile setup](PROFILE.md#first-run). Serialize setup per profile. Read project settings from that profile. If any required choices are missing, use the [setup interview](PROFILE.md#setup-interview) and continue after the answers are saved and validated. Install or verify all three skills. Use [project-manager commands in Hermes](COMMANDS.md) for diagnostics and recovery.
2. Run `check` to verify that the configured Herdr session is reachable and that GitHub identities, canonical repository IDs, names and HTTPS clone endpoints, and initialized wiki HEADs for the dashboard and owned repositories are valid. Check the scoped layout if a workspace is already bound.
3. Prepare and enable one Hermes cron watchdog job with `--no-agent` in the selected profile. Reuse or start a Hermes scheduler that serves the profile and will keep running after the opening session exits. Reuse a matching job on later launches and keep overlapping managers paused. Trigger the registered job immediately on first setup. See [Hermes cron operations](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron#lifecycle-actions).
4. The watchdog calls [pane preparation](#prepare-panes) on its first run and on each scheduled run. Report first run as ready once the manager has a fresh heartbeat and the enabled watchdog has a successful recorded run. Report any remaining dispatch hold separately. Verify the latest in-pane pass with `show`. The in-pane supervisor owns periodic project work.
5. In each pass, read labeled GitHub issues and host state. With dry run off, reconcile saved work, admit a bounded amount of new work, and publish eligible status. Serialize passes and direct mutations with profile locks. `follow-up` and `capability-gap` require a verified live supervisor on the clean installed revision and dry run off. Recheck these conditions before each effect. A pending update or restart holds these mutations.

## Prepare panes

`/project-manager prepare-panes` and the watchdog call the same scripted operation with the selected profile settings. The operation uses fixed pane roles and launch commands through the pane manager and agent manager APIs. Serialize preparation per profile so overlapping manual and scheduled runs reuse the same panes.

1. Reuse the verified workspace or create it in the configured local Herdr session. Save the exact workspace ID. Verify the scoped layout and all saved pane bindings before creating anything in the workspace.
2. If the worker backend is Codex and quota checking is enabled, verify or provision the dedicated quota receiver. Save its pane ID and verify that its observations are for the worker account. If quota is unavailable, report a dispatch hold while the manager continues to run.
3. Verify the manager pane and heartbeat. Start the manager if the saved pane is absent and creation is safe. Journal creation, confirm the exact pane in the workspace, and verify its fresh heartbeat. Report a hold when pane identity, locks or a previous launch cannot be verified.
4. Report the workspace, fixed panes and any missing worker panes from tracked assignments. Worker panes are provisioned when assigning work. Preserve the saved assignment when its pane is missing so the manager can reconcile it.

## Dry run

When `dry_run` is `true`, the manager reads labeled issues, layout, versions and quota when enabled, and saves a local report of what it would do. View that report through `/project-manager show`. With dry run on, the manager skips worker assignments and follow-ups, GitHub writes, task state changes, cleanup, and skill updates. The manager and watchdog keep running, and work already submitted can finish.

Use [the dry-run command](COMMANDS.md#dry-run) to change the profile setting while the manager is running. Re-read the setting for each pass and before actions it controls. Recheck profile readiness before turning dry run off. The manager uses the new setting in the same pane.

## Route GitHub issues

- The manager reads open issues in each configured repository. An authorized triager assigns an issue by applying the manager's label, `hermes:<name>`, in that repository. Accept an issue when it carries that label as its sole manager label.
- The issue's repository is the worker's target. Give the worker's assigned app account the GitHub access needed for the job. The dashboard reports the status of assignments across repositories.
- Save the immutable repository and issue IDs with the assignment. The human-readable selector is `OWNER/REPO#N`. Revalidate the binding and manager label before assigning, following up, or posting dashboard status. A transfer, replaced repository, conflicting labels, or assignment without a verifiable binding goes to human attention. Coordinate label changes while a worker is in flight. Relabeling blocks future submissions under the old assignment and does not terminate the accepted turn.
- Keep one live worker assignment per bound issue. Repeated discovery or a restart must reuse its existing evidence. Process eligible issues in stable issue-key order within the configured per-pass and worker capacity limits. Count starting, active and unresolved worker submissions against capacity.

## Quota check

With `quota_check: true`, the project manager evaluates fresh rate-limit windows before each new Codex turn, including idle follow-ups. Request the windows through the agent manager's public `get-remaining-tokens --agent codex` operation. It reports remaining percentage, window duration and reset time. Use observations for the account and limit bucket of the intended receiver.

The rule is fixed. In each usable window, the allowed usage is the percentage of the window that has elapsed. Hold a new turn if actual usage exceeds that allowance in any usable window. At 40% of the way through a window, 40% usage is allowed and 41% usage is held. A usable window has a finite remaining percentage between 0 and 100, a positive duration, and a future reset within that duration. If there are no usable windows, report that quota is unavailable and leave new turns waiting. Re-evaluate with fresh readings before a later submission.

Use [the quota-check command](COMMANDS.md#quota-check) to change the setting. With `quota_check: false`, the project manager skips quota readings and pacing. The agent manager applies the project manager's decision to each submission and still checks transport, identity, locks and turn binding. Provider limits continue to apply.

## Assign work

1. Check that dry run is off and verify the route, repository identity, host CPU and disk, worker capacity, and any applicable quota before provisioning. Prepare a separate working directory for the assignment and save its identity, the target base, and a bounded issue snapshot. Enable the network access needed for the worker to pull and push on GitHub. The prompt specifies the job scope, the PR or target base, the assigned GitHub App account, acceptance criteria, and the expected response. The worker handles review requests within the job through authorized channels. If a request needs a human action or access the worker lacks, the worker reports the action in its response. Pass issue content as data and pass prompts on stdin.
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

The prompt requires the worker to use only the GitHub App set in `worker.github_identity` for GitHub access. In this deployment that account is `milyin-hermes` (`milyin-hermes[bot]` on GitHub). The worker uses the configured app access for Git and GitHub operations and reports an access blocker if it cannot proceed under that identity. The agent manager binds GitHub access to the assigned App identity and prevents fallback to the operator's personal GitHub account.

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

Make changes to each skill through a reviewed PR in its own repository. Use `capability-gap SKILL short-slug` for a verified dependency gap, then assign the job through the ordinary worker workflow. Include any required review requests in that assignment. Preserve a blocked candidate and its evidence.

Read the canonical `main` refs at startup and at the next pass after `schedule.skill_update_interval_seconds` elapses, default 1800. `versions` is a read-only probe. With dry run off, attempt updates in dependency order, pane manager, agent manager, then project manager. Use only a clean same-profile `main` checkout at a quiet checkpoint without unfinished turns or candidates. Require readable unlocked Curator state, verified origin and local execution files, and trusted push CI for the exact remote head. Use the same checks for one-shot `sync SKILL --approved-head SHA` with the supervisor stopped.

Every new first-parent commit in the update range must correspond to a PR merged into that repository's `main`, with the reviewed head's tree and the same reviewed base. Require a single `Hermes-Update-Base: BASE_SHA` line in the reviewed head commit and verified independent approval of that head and range before merge. Hold on direct pushes, insufficient evidence, locked or dirty installs, or potential file collisions. Fast-forward only. Self-update requires current dependencies. Preserve the Hermes pane and hold dispatch until a verified restart loads the new revision and reconciles any `skill-update.pending.json` receipt.

## Acceptance criteria

Verify conformance through the ordinary entry points. Cover dry run without external writes or worker starts, toggling dry run in the same manager pane, commands with and without a live bound manager, matching and conflicting manager labels, quota and host refusal before submission, duplicate passes, ambiguous effects across restart, and both follow-up bindings. Exercise successful and failed worker responses with and without human action, malformed responses, wiki disclosure and update holds. Verify that the prompt names the assigned GitHub App and that the worker can pull, push, and update the PR. Verify that worker GitHub access is bound to the assigned App account. Distinguish unit and simulated tests, hosted CI, and live same-profile verification.

Verify first run by opening a configured profile and sending no message. Check the new workspace, live manager pane and successful run of the enabled watchdog. Reopen the profile and verify that it reuses them. After closing the opening session, verify that the scheduler still runs the watchdog. For a project-manager profile with missing settings, verify that the interview requests only missing choices and continues startup after they are saved and validated.

Verify that the Hermes command and watchdog prepare the same panes, including repeated runs and recovery after a known pane disappears. Test quota checking on and off, with usage below, equal to and above elapsed allowance.
