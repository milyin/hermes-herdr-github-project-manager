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

4. Run the [installer](install_proxy.py) to create a wrapper named for the App slug in a trusted profile-owned directory visible to all three roles. Use the absolute path printed by the installer, not the bare slug or a `PATH` override. Use a shell-safe path without whitespace for the prompt examples. The wrapper invokes the bundled launcher with the fixed JSON path, independent of `XDG_CONFIG_HOME`. Keep the workflow directory intact and make Python 3.10+, OpenSSL, Git, and GitHub CLI available where the wrapper runs.

   ```sh
   python3 -B /absolute/path/to/skill/workflows/github/install_proxy.py \
     --config /absolute/path/to/your-app-slug.json --bin-dir /absolute/path/to/profile/bin
   ```

5. Confirm the repository wiki is available, enabled, and initialized with a first page. A private repository needs a plan that includes wikis. [GitHub wiki availability](https://docs.github.com/en/communities/documenting-your-project-with-wikis)
6. Confirm the repository, App, and opt-in label with the operator. Use the bundled label helper to find or create the label. Save the returned name as the manager's `LABEL` parameter. The operator controls which issues carry the opt-in label.

   ```bash
   python3 -B /absolute/path/to/skill/workflows/github/ensure_github_label.py -- OWNER/REPO /absolute/path/to/profile/bin/your-app-slug 'hermes:projectmanager'
   ```

7. Fill in the [profile settings](#profile-settings), save the operator-confirmed profile, and run `/project-manager`. With the operator, verify the workflow on an opted-in issue.

## Using the launcher

Set `APP_WRAPPER` to the absolute path printed by the installer. Pass `--repo OWNER/REPO` before the action. Use `gh` for supported issue and PR commands and repository REST endpoints. Put the endpoint first after `gh api`. Use numeric issue and PR selectors and omit `gh -R` or `gh --repo`; the wrapper supplies the repository. Pass `--head` to `gh pr create` after pushing through the wrapper. Use the checked `merge-reviewed` action for direct merges. If the pinned wrapper fails, report the blocker rather than using a different launcher or identity.

Pass the exact credential-free HTTPS repository or wiki URL to remote Git commands. Run the command from the intended checkout. Push an explicit branch refspec. For example:

```bash
"$APP_WRAPPER" --repo OWNER/REPO git fetch https://github.com/OWNER/REPO.git refs/pull/42/head
"$APP_WRAPPER" --repo OWNER/REPO git push https://github.com/OWNER/REPO.git HEAD:refs/heads/issue-42
```

Each invocation verifies the configured App and repository before dispatch. Use `"$APP_WRAPPER" --repo OWNER/REPO status` to inspect the actor, repository, and permissions. Native Git and GitHub CLI commands use isolated configuration and repository-scoped App credentials. The launcher scopes the operations it handles, and the workflow prompts require its use. Use operator-managed execution controls if a session must be restricted to the launcher.

GitHub's merge readiness and active branch rules are checked before the direct merge. The workflow also requires a clean worker verdict, cleared actionable feedback, and an active independent human approval for the exact head. The manager supplies the verdict and feedback decision from verified workflow records. The SHA pin guards against head changes. Review state can still change between the final read and the write. GitHub branch rules supply any server-side review enforcement. [Merge instructions](MANAGER_PROMPT.md#merging-conditions) · [GitHub merge endpoint](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)

## Worker handoffs

The manager sends self-contained assignments in the worker's managed terminal. A review assignment carries the issue, PR, exact head, and implementation evidence. An implementation follow-up carries the references, reviewed head, actual findings, supporting details, and requested changes. Pane, turn and feedback IDs stay with the manager for tracking. The wiki records a short status and the identifiers needed to resume work. [Worker handoff boundary](../../doc/WORKFLOW.md#worker-handoff-boundary)

For an oversized assignment, the manager can reuse an eligible idle pane if the complete follow-up fits, or assign independent implementation findings one at a time. An indivisible assignment that exceeds the limit stays blocked until the runtime can admit the complete evidence. [Oversized handoff recovery](../../doc/WORKFLOW.md#oversized-worker-handoffs)

The reviewer publishes a submitted GitHub comment review pinned to the reviewed SHA and returns a short terminal notification. This is ordinary PR review publication. The manager verifies the publication, reads the findings, and includes their text in the implementer's terminal prompt. The App's review records the worker verdict. Approval still comes from an independent human reviewer on the exact current head. [Reviewer prompt](REVIEWER_PROMPT.md) · [GitHub review endpoint](https://docs.github.com/en/rest/pulls/reviews#create-a-review-for-a-pull-request)

## Profile settings

Replace the models, working directories, repository, label, and absolute App wrapper path below. Set all three `github_identity` values to the same installer output. The manager passes it to both workers; matching defaults cover manual runs.

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
          github_identity: /absolute/path/to/profile/bin/your-app-slug
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
            github_identity: /absolute/path/to/profile/bin/your-app-slug
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
            github_identity: /absolute/path/to/profile/bin/your-app-slug
```

## Update an existing GitHub profile

Start with the [installed profile upgrade checklist](../../doc/PROFILE.md#upgrade-an-installed-profile). Keep the watchdog paused during launcher and prompt migration.

If the existing App JSON uses the legacy fields below, create a separate JSON file outside the skill. Use `umask 077` before creating it and keep it owned by the running user with mode `0600`. Populate it with these four fields.

| Legacy field | Proxy field | Value |
| --- | --- | --- |
| `expected_slug` | `app_slug` | The same App slug |
| `app_id` | `app_id` | The same GitHub App ID |
| `default_repository` | `repository` | The profile's verified `OWNER/REPO`; the legacy default may differ |
| `key_path` | `private_key` | The absolute path to the existing private key file |

Reuse the existing key in place. Put only its path in `private_key`, and keep the key file private and owned by the running user. If the old value starts with `~/`, expand it using the original user's home directory. Resolve a relative path from the working directory the legacy launcher used, and use the path to the regular key file if the old path is a symlink.

Pass the new JSON's absolute path to `install_proxy.py --config` as shown in step 4. A JSON file that already uses the proxy fields can be passed directly. If a shared bin directory already contains that App slug, install into a separate trusted profile directory. Set all three `github_identity` values to the new absolute wrapper path without changing the shared launcher. Previously sent worker prompts are not updated by settings changes; use a fresh assignment or explicitly supply the new path in a follow-up.

If any of the three `prompt_file` values still use `doc/`, replace only those values. These examples are relative to the profile directory. For absolute paths, keep the checkout prefix and replace the part after it.

| Previous path suffix | Current path suffix |
| --- | --- |
| `doc/MANAGER_PROMPT.md` | `workflows/github/MANAGER_PROMPT.md` |
| `doc/IMPLEMENTER_PROMPT.md` | `workflows/github/IMPLEMENTER_PROMPT.md` |
| `doc/REVIEWER_PROMPT.md` | `workflows/github/REVIEWER_PROMPT.md` |

Preserve the other manager and worker parameters, models, working directories, launch profiles, and pane settings. Read back the three prompt paths and verify that the files resolve in the selected profile.

Before resuming, verify scoped `status`, a read-only GitHub command, and a read-only Git command with the exact wrapper in the manager and worker runtimes. From the manager, check that `"$APP_WRAPPER" --repo OWNER/REPO merge-reviewed --help` advertises `--reviewed-head`. A legacy launcher can pass `status` but lack the checked merge command. If a check fails, finish the proxy migration and repeat the checks; do not fall back to a shared launcher, substitute a raw merge request, or test by merging a live PR.
