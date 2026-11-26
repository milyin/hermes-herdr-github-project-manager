# GitHub workflow

The manager tracks opted-in issues, delegates implementation and review, and publishes a wiki dashboard. The [manager](MANAGER_PROMPT.md), [implementer](IMPLEMENTER_PROMPT.md), and [reviewer](REVIEWER_PROMPT.md) use an installed App proxy for the selected repository.

## Install the workflow

1. Follow the [main installation](../../doc/PROFILE.md#install-and-open-the-profile) and choose this workflow in the setup interview. Confirm one GitHub.com repository as `OWNER/REPO` and the slug of an App the operator controls.
2. [Register or choose a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app) and [install it on the selected repository](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app). Give it **Contents**, **Issues**, and **Pull requests** read/write access, and **Checks** and **Commit statuses** read access. Keep branch-rule enforcement enabled for the App. Use the workflow's existing narrow permissions if they meet these requirements.
3. [Generate a private key](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps). Save the key and a JSON configuration outside the skill, in a directory accessible to the manager and workers. Both files must be regular files owned by the running user with permission `0600`. Use the App ID shown in GitHub's App settings and an absolute key path. Keep credentials out of profiles, prompts, transcripts, and Git remotes.

   ```json
   {
     "app_slug": "your-app-slug",
     "app_id": 12345,
     "repository": "owner/repository",
     "private_key": "/absolute/path/to/app-private-key.pem"
   }
   ```

4. Run the [installer](install_proxy.py) to create a command named for the configured App slug. Put it in a trusted directory on the manager and both workers' `PATH`. The proxy uses the absolute JSON path passed to the installer, regardless of the process's `XDG_CONFIG_HOME`. Keep the workflow directory intact and make Python 3.10+, OpenSSL, Git, and GitHub CLI available where the proxy runs.

   ```sh
   python3 -B /absolute/path/to/skill/workflows/github/install_proxy.py \
     --config /absolute/path/to/your-app-slug.json --bin-dir "$HOME/.local/bin"
   ```

5. Confirm the repository wiki is available, enabled, and initialized with a first page. A private repository needs a plan that includes wikis. [GitHub wiki availability](https://docs.github.com/en/communities/documenting-your-project-with-wikis)
6. Confirm the repository, App, and opt-in label with the operator. Use the bundled label helper to find or create the label. Save the returned name as the manager's `LABEL` parameter. The operator controls which issues carry the opt-in label.

   ```bash
   python3 -B /absolute/path/to/skill/workflows/github/ensure_github_label.py -- OWNER/REPO APP_SLUG 'hermes:projectmanager'
   ```

7. Fill in the [profile settings](#profile-settings), save the operator-confirmed profile, and run `/project-manager`. With the operator, verify the workflow on an opted-in issue.

## Using the launcher

Pass `--repo OWNER/REPO` before the action. Use `gh` for supported issue and PR commands and repository REST endpoints. Put the endpoint first after `gh api`. Use numeric issue and PR selectors for CLI commands, and pass `--head` to `gh pr create` after pushing through the launcher. Use the checked `merge-reviewed` action for direct merges. The manager uses `pr-facts` for an exact-head PR snapshot and `schedule` for GitHub workflow decisions.

Pass the exact credential-free HTTPS repository or wiki URL to remote Git commands. Run the command from the intended checkout. Push an explicit branch refspec. For example:

```bash
APP_SLUG --repo OWNER/REPO git fetch https://github.com/OWNER/REPO.git refs/pull/42/head
APP_SLUG --repo OWNER/REPO git push https://github.com/OWNER/REPO.git HEAD:refs/heads/issue-42
```

Each invocation verifies the configured App and repository before dispatch. Use `APP_SLUG --repo OWNER/REPO status` to inspect the actor, repository, and permissions. Native Git and GitHub CLI commands use isolated configuration and repository-scoped App credentials. The launcher scopes the operations it handles, and the workflow prompts require its use. Use operator-managed execution controls if a session must be restricted to the launcher.

GitHub's merge readiness and active branch rules are checked before the direct merge. The workflow also requires a clean worker verdict, cleared actionable feedback, and an active independent human approval for the exact head. The manager supplies the verdict and feedback decision from verified workflow records. The SHA pin guards against head changes. Review state can still change between the final read and the write. GitHub branch rules supply any server-side review enforcement. [Merge instructions](MANAGER_PROMPT.md#merging-conditions) · [GitHub merge endpoint](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)

## Dashboard queues

The dashboard lists **Need attention**, **In progress**, **In review**, and **Done** in that order. Each issue has one entry. Ready implementation work and follow-ups wait under **In progress → Queue**, and ready PR heads wait under **In review → Queue** when all worker slots are occupied. Operator blockers and PRs queued or scheduled for merge in GitHub stay in **Need attention**.

**Awaiting human approval** holds merge attempts for the exact PR head with a clean worker verdict and clear feedback. A changed head or new actionable feedback clears that hold so the manager can schedule the required review or implementation follow-up.

The manager uses the same scheduler on a periodic turn and after recording a worker result. Eligible work already queued at the beginning of the turn goes first, followed by other tracked work and then new issues on periodic turns. The manager saves and acknowledges the exact result before scheduling. It verifies admission before moving the entry to the active list and recording the admitted feedback as assigned. Pending feedback and interrupted dispatch identifiers stay on the same issue entry. [Shared scheduling](MANAGER_PROMPT.md#shared-scheduling)

## Scheduler input

Run `APP_SLUG --repo OWNER/REPO pr-facts --pr NUMBER` to obtain the `pull` record for an entry. The scoped App read includes the PR's full current head, state, draft status, mergeability, and current merge-queue and auto-merge facts. `CONFLICTING` blocks merge. `UNKNOWN` waits for verification. A fresh `MERGEABLE` snapshot clears the mergeability blocker. Worker review and implementation can proceed subject to the dispatch rules.

Run `APP_SLUG --repo OWNER/REPO schedule --implementer NAME --reviewer NAME` with one JSON object on stdin. Use the worker names from the profile. The command returns decisions as JSON. The manager applies them using its existing wiki and worker tools.

| Request field | Source |
| --- | --- |
| `event` | `periodic` or `worker_result`. |
| `queued_at_start` | Ordered numeric issue IDs from both queues, captured before changing the dashboard and reused throughout the turn. |
| `new_intake` | Numeric IDs of newly discovered issues on a periodic turn, even if an entry for one of them is created during the turn. |
| `attempted` | Numeric issue IDs already sent to `run` or `steer` this turn. Start with an empty list. |
| `capacity` | Latest `result` from a successful `project_manager_capacity` call, including `available` and `workers`. Refresh after each attempt and after checking an intended checkout. |
| `worker_result` | On a worker-result turn, the verified event's `pane_id` and `turn_id`. Set `recorded: true` after saving the result and its evidence. Set `acknowledgment` to the full `project_manager_mark_read` response once available. |
| `entries` | One record per numeric issue, in dashboard order followed by new intake. |

| Entry field | Source |
| --- | --- |
| `issue`, `placement`, `eligible` | Numeric issue ID, exact dashboard section name (including ` → Queue` as applicable), and verified issue eligibility. Use `placement: null` for new intake after checking the dashboard and worker assignments. |
| `pr`, `pull` | Numeric current PR ID if known and the verified `pr-facts` record for that PR. |
| `verdict` | Verified worker `pr`, full `head`, and `verdict` (`clean` or `changes_requested`). Preserve the old verdict as evidence when the target head changes. |
| `approval_hold` | The saved `pr` and full `head` from **Awaiting human approval**. For a legacy display status, pass the saved PR, or `{}` if the entry's PR is the only saved scope. The helper can recover the SHA from a verified clean verdict for that PR. |
| `merge_check` | The checked merge blocker with `pr`, `head`, `reason`, and optional `outcome`. Keep an unconfirmed write until GitHub confirms the outcome or verifies that the original write did not take effect. |
| `feedback`, `pending_feedback`, `assigned_feedback` | Your interpretation of verified feedback (`clear`, `actionable`, or `unknown`), plus the pending and assigned lists of unique string IDs. Preserve source references on the dashboard. |
| `pending_work` | `implementation` or `review` for explicit follow-up work, or null. |
| `active_assignment`, `blocker` | Current verified assignment, with pane, turn and head when applicable, or null. Supply any verified worker or operator blocker as a short status string. Pass mergeability through `pull`. |
| `handoff` | `kind`, `pr`, `head` (use null for a missing PR or head), and `feedback_ids` for the complete prepared prompt. Use null until the prompt and evidence match this work. For a run, `checkout` is the `result.preflight` from `project_manager_capacity` for the selected worker and working directory. An optional `steer` object contains the verified `issue`, `worker`, `pane_id`, `thread_id`, `turn_id` (null if no prior turn), `lock`, `state`, `pending_closure`, and `quota_ready` from a verified quota observation. |
| `dispatch`, `receipt`, `recovery` | Persist the selected dispatch object before using the worker tool. Pass the entire tool response as `receipt` for that recorded attempt. Use `recovery` only for verified evidence from the original binding. |

The scheduler may return `prepare_checkout` for the highest-priority item before it can authorize a run. Check the intended checkout and resubmit the snapshot. A `dispatch` for a run includes the preflighted `working_directory`. An open draft can receive eligible implementation follow-ups on its exact head. Review and merge wait until the PR is ready for review.

A recovery record includes `issue`, `operation`, `worker`, `pr`, `head`, and `state` (`admitted` or `not_attempted`). For `run`, include the original `working_directory`. For `steer`, include the original `pane_id`, `thread_id`, and `previous_turn_id`. An admitted attempt includes the verified `pane_id` and `turn_id`, plus `thread_id` if the target binding is known. For a verified unsubmitted launch, set `launch_cleared: true` only after resolving any retained pane. A missing pane or a previous turn by itself leaves the original attempt unresolved.

The top-level `dispatch` is the only authorization for the next worker call. `decisions` carry the issue placement, any status update, approval hold, mergeability blocker, pending target, and action. Keep the saved handoff evidence and feedback lists unless a verified admission supplies replacements. A `pending_work` record is a target for the handoff writer. Retain an unresolved `dispatch` until a decision returns `clear_dispatch: true`. Before a worker result is recorded and acknowledged, decisions can reconcile existing attempts, including an admission first verified by that result. Save the reconciliation before recording and acknowledging the result. A `merge_candidate` is checked by `merge-reviewed` before GitHub receives a merge write.

## Worker handoffs

The manager sends self-contained assignments in the worker's managed terminal. A review assignment carries the issue, PR, exact head, and implementation evidence. An implementation follow-up carries the references, reviewed head, actual findings, supporting details, and requested changes. Pane, turn and feedback IDs stay with the manager for tracking. The wiki records a short status and the identifiers needed to resume work. [Worker handoff boundary](../../doc/WORKFLOW.md#worker-handoff-boundary)

For an oversized assignment, the manager can reuse an eligible idle pane if the complete follow-up fits, or assign independent implementation findings one at a time. An indivisible assignment that exceeds the limit stays blocked until the runtime can admit the complete evidence. [Oversized handoff recovery](../../doc/WORKFLOW.md#oversized-worker-handoffs)

The reviewer publishes a submitted GitHub comment review pinned to the reviewed SHA and returns a short terminal notification. This is ordinary PR review publication. The manager verifies the publication, reads the findings, and includes their text in the implementer's terminal prompt. The App's review records the worker verdict. Approval still comes from an independent human reviewer on the exact current head. [Reviewer prompt](REVIEWER_PROMPT.md) · [GitHub review endpoint](https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request)

## Profile settings

Replace the models, working directories, repository, label, and App slug below. The manager passes its selected identity to both workers. Keep worker defaults in sync for manual runs.

```yaml
plugins:
  enabled:
    - hermes-herdr-github-project-manager

skills:
  config:
    hermes-herdr-github-project-manager:
      enabled: true
      herdr:
        instance: default
      max_working_panes: 1
      manager:
        name: projectmanager
        model: YOUR_HERMES_MODEL
        working_directory: /absolute/path/to/manager
        description: Manage the repository and its wiki dashboard
        prompt_file: skills/hermes-herdr-github-project-manager/workflows/github/MANAGER_PROMPT.md
        parameters:
          REPO: owner/repository
          LABEL: hermes:projectmanager
          IMPLEMENTER: implementer
          REVIEWER: reviewer
          github_identity: your-app-slug
      workers:
        - name: implementer
          pane_tag: I
          agent: codex
          model: YOUR_CODEX_MODEL
          working_directory: /absolute/path/to/implementer-checkout
          description: Implement issues and address PR feedback
          prompt_file: skills/hermes-herdr-github-project-manager/workflows/github/IMPLEMENTER_PROMPT.md
          parameters:
            REPO: null
            ISSUE: none
            PR: none
            TASK: null
            github_identity: your-app-slug
        - name: reviewer
          pane_tag: R
          agent: codex
          model: YOUR_CODEX_MODEL
          working_directory: /absolute/path/to/reviewer-checkout
          description: Review the exact PR head and report findings
          prompt_file: skills/hermes-herdr-github-project-manager/workflows/github/REVIEWER_PROMPT.md
          parameters:
            REPO: null
            ISSUE: null
            PR: null
            HEAD: null
            github_identity: your-app-slug
```

## Update an existing GitHub profile

If the existing App JSON uses the legacy fields below, create a separate JSON file outside the skill. Use `umask 077` before creating it and keep it owned by the running user with mode `0600`. Populate it with these four fields.

| Legacy field | Proxy field | Value |
| --- | --- | --- |
| `expected_slug` | `app_slug` | The same App slug |
| `app_id` | `app_id` | The same GitHub App ID |
| `default_repository` | `repository` | The same `OWNER/REPO` |
| `key_path` | `private_key` | The absolute path to the existing private key file |

Reuse the existing key in place. Put only its path in `private_key`, and keep the key file private and owned by the running user. If the old value starts with `~/`, expand it using the original user's home directory. Resolve a relative path from the working directory the legacy launcher used, and use the path to the regular key file if the old path is a symlink.

Pass the new JSON's absolute path to `install_proxy.py --config` as shown in step 4. A JSON file that already uses the proxy fields can be passed directly. If the destination already contains a command with the same App slug, use a trusted directory earlier on the workflow's `PATH` and ensure the manager and workers resolve the new proxy.

If any of the three `prompt_file` values still use `doc/`, replace only those values. These examples are relative to the profile directory. For absolute paths, keep the checkout prefix and replace the part after it.

| Previous path suffix | Current path suffix |
| --- | --- |
| `doc/MANAGER_PROMPT.md` | `workflows/github/MANAGER_PROMPT.md` |
| `doc/IMPLEMENTER_PROMPT.md` | `workflows/github/IMPLEMENTER_PROMPT.md` |
| `doc/REVIEWER_PROMPT.md` | `workflows/github/REVIEWER_PROMPT.md` |

Keep the existing manager and worker records, parameter values, models, working directories, launch profiles, and pane settings. Run `/project-manager` in that profile to validate the configuration.
