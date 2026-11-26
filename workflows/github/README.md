# GitHub workflow

The manager tracks opted-in issues, delegates implementation and review, and publishes a wiki dashboard. The [manager](MANAGER_PROMPT.md), [implementer](IMPLEMENTER_PROMPT.md), and [reviewer](REVIEWER_PROMPT.md) use the bundled App launcher for the selected repository.

## Install the workflow

1. Follow the [main installation](../../doc/PROFILE.md#install-and-open-the-profile) and choose this workflow in the setup interview. Confirm one GitHub.com repository as `OWNER/REPO` and the slug of an App the operator controls.
2. [Register or choose a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app) and [install it on the selected repository](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app). Give it **Contents**, **Issues**, and **Pull requests** read/write access, and **Checks** and **Commit statuses** read access. Keep branch-rule enforcement enabled for the App. Use the workflow's existing narrow permissions if they meet these requirements.
3. Install the [bundled launcher](github_app.py) as a command named for the App slug in a trusted directory on the manager and both workers' `PATH`. It needs Python 3.10+, OpenSSL, Git, and GitHub CLI. Keep the workflow directory intact so the launcher can load the merge policy. For example:

   ```bash
   workflow=/absolute/path/to/skill/workflows/github
   app_slug=your-app-slug
   mkdir -p "$HOME/.local/bin"
   ln -s "$workflow/github_app.py" "$HOME/.local/bin/$app_slug"
   ```

   Ensure the workflow's `PATH` resolves the App slug to the bundled launcher.
4. [Generate a private key](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps). Save the key and a JSON configuration outside the skill, in a directory accessible to the operator's workflow environments. Both files must be regular files owned by the running user with permission `0600`. The default configuration is `$XDG_CONFIG_HOME/herdr-project-manager/github/APP_SLUG.json`, or `~/.config/herdr-project-manager/github/APP_SLUG.json`. Use the App ID shown in GitHub's App settings.

   ```json
   {
     "app_slug": "your-app-slug",
     "app_id": 12345,
     "repository": "owner/repository",
     "private_key": "/absolute/path/to/app-private-key.pem"
   }
   ```

5. Verify the selected App and repository. Require `authenticated: true`, the expected `APP_SLUG[bot]` actor and repository, and the permissions from step 2. Keep credentials out of profiles, prompts, transcripts, and Git remotes.

   ```sh
   APP_SLUG --repo OWNER/REPO status
   ```

6. Confirm the repository wiki is available, enabled, and initialized with a first page. A private repository needs a plan that includes wikis. Verify wiki read access and verify `permissions.contents: write` in the status result. [GitHub wiki availability](https://docs.github.com/en/communities/documenting-your-project-with-wikis)

   ```bash
   APP_SLUG --repo OWNER/REPO git ls-remote https://github.com/OWNER/REPO.wiki.git
   ```

7. Confirm the repository, App, and opt-in label with the operator. Use the bundled label helper to find or create the label. Save the returned name as the manager's `LABEL` parameter. The operator controls which issues carry the opt-in label.

   ```bash
   python3 -B /absolute/path/to/skill/workflows/github/ensure_github_label.py -- OWNER/REPO APP_SLUG 'hermes:projectmanager'
   ```

8. Fill in the [profile settings](#profile-settings), save the operator-confirmed profile, and run `/project-manager`. With the operator, verify the workflow on an opted-in issue.

## Using the launcher

The profile's [setup hook](../../doc/PROFILE.md#environment-setup-hooks) checks `merge-reviewed --help` for `--reviewed-head`, verifies the configured App, and supplies the checked launcher path to each managed turn. If the launcher fails either check, the engine holds the turn and reports the reason.

Pass `--repo OWNER/REPO` before the action. Use `gh` for supported issue and PR commands and repository REST endpoints. Put the endpoint first after `gh api`. Use numeric issue and PR selectors for CLI commands, and pass `--head` to `gh pr create` after pushing through the launcher. Use the checked `merge-reviewed` action for direct merges.

Pass the exact credential-free HTTPS repository or wiki URL to remote Git commands. Run the command from the intended checkout. Push an explicit branch refspec. For example:

```bash
APP_SLUG --repo OWNER/REPO git fetch https://github.com/OWNER/REPO.git refs/pull/42/head
APP_SLUG --repo OWNER/REPO git push https://github.com/OWNER/REPO.git HEAD:refs/heads/issue-42
```

The launcher verifies the configured App and mints credentials for the selected repository. Native Git and GitHub CLI commands use isolated configuration and App credentials. The launcher scopes the operations it handles, and the workflow prompts require its use. Use operator-managed execution controls if a session must be restricted to the launcher.

GitHub's merge readiness and active branch rules are checked before the direct merge. The workflow also requires a clean worker verdict, cleared actionable feedback, and an active independent human approval for the exact head. The manager supplies the verdict and feedback decision from verified workflow records. The SHA pin guards against head changes. Review state can still change between the final read and the write. GitHub branch rules supply any server-side review enforcement. [Merge instructions](MANAGER_PROMPT.md#merging-conditions) · [GitHub merge endpoint](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request)

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
      setup_hook: skills/hermes-herdr-github-project-manager/workflows/github/setup_environment.py
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

Back up the selected profile's `config.yaml`. In `skills.config.hermes-herdr-github-project-manager`, add this field alongside `manager` and `workers`.

```yaml
setup_hook: skills/hermes-herdr-github-project-manager/workflows/github/setup_environment.py
```

If any of the three `prompt_file` values still use `doc/`, replace only those values. These examples are relative to the profile directory. For absolute paths, keep the checkout prefix and replace the part after it.

| Previous path suffix | Current path suffix |
| --- | --- |
| `doc/MANAGER_PROMPT.md` | `workflows/github/MANAGER_PROMPT.md` |
| `doc/IMPLEMENTER_PROMPT.md` | `workflows/github/IMPLEMENTER_PROMPT.md` |
| `doc/REVIEWER_PROMPT.md` | `workflows/github/REVIEWER_PROMPT.md` |

Keep the existing manager and worker records, parameter values, App configuration, models, working directories, launch profiles, and pane settings. Run `/project-manager` in that profile to validate the edited configuration. A managed turn starts only after its [bound runtime verifies the hook](../../doc/PROFILE.md#environment-setup-hooks).
