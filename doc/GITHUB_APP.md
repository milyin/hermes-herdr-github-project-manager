# GitHub App setup

The bundled GitHub workflow uses an App installation on one repository. The manager, implementer, and reviewer invoke a scoped authentication helper. `github_identity` is the App slug and the executable name on `PATH`. The examples use `milyin-hermes`. For another App, install a trusted helper named for its slug that supports the same `--repo OWNER/REPO status`, `gh`, and `git` interface. The helper must use installation credentials and fail if it cannot authenticate as the selected App.

1. Register an App with the owner of the target repository, or select an existing App. Give it repository **Contents**, **Issues**, and **Pull requests** read/write access for Git and wiki updates, opt-in labels, and PRs. Give it **Checks** and **Commit statuses** read access to inspect merge gates. Install it on the target repository. [Register a GitHub App](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/registering-a-github-app) · [Install your GitHub App](https://docs.github.com/en/apps/using-github-apps/installing-your-own-github-app)
2. Generate a private key and configure the helper outside the project-manager profile settings. For `milyin-hermes`, make `bin/milyin-hermes` from the tooling checkout available on `PATH` and keep the checkout in place. The helper's private configuration defaults to `~/.config/milyin-hermes/config.json`. Set `account` to the owner, `default_repository` to `OWNER/REPO`, `expected_slug` to the App slug, and `app_id` to the numeric App ID as a string. Set `key_path` and `cache_directory` to protected local paths, and `real_gh` and `real_git` to the absolute paths of the trusted native executables. Keep the configuration, key, and token cache outside repositories and prompts. [Launcher source](https://github.com/milyin/milyin-hermes) · [Manage GitHub App private keys](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps)
3. Make the helper available to the Hermes gateway and both Codex worker environments. Set the App slug as `github_identity` in the manager and both worker parameter mappings in the [selected profile](PROFILE.md#settings). Use credential-free HTTPS remotes for the repository and its wiki.
4. Run the status command below. Before continuing, verify `authenticated: true`, the expected `APP_SLUG[bot]` actor, the selected repository, and `contents: write` under `permissions`. Use the wrapper for all Git and GitHub operations. Pass `--repo` to `gh` commands that accept it so the target is explicit.

```bash
milyin-hermes --repo OWNER/REPO status
milyin-hermes --repo OWNER/REPO gh issue list --repo OWNER/REPO --label 'hermes:projectmanager'
milyin-hermes --repo OWNER/REPO git ls-remote https://github.com/OWNER/REPO.git
milyin-hermes --repo OWNER/REPO git ls-remote https://github.com/OWNER/REPO.wiki.git
```

The wiki `git ls-remote` command verifies read access only. The scoped App status supplies the required write permission check. Keep the main repository as the wrapper scope for wiki Git operations. If the helper is missing, the status check fails, or the App is denied access, stop the affected work and report the failure. Do not fall back to a personal login, token, credential helper, or GitHub connector.

The bundled workflow reads PR reviews and collaborator permissions before a managed merge. An unavailable read leaves the PR in **Need attention** with **Awaiting human approval**. The manager submits a direct merge pinned to the approved SHA after verifying the approval and applicable rules. [Manager merges](WORKFLOW.md#manager-merges)

## Opt-in label

Run this command from the installed project-manager checkout after the operator confirms the repository, App, and label. Substitute the operator's selected values. The script rechecks the App identity, repository, and Contents write permission before reading all label pages and creating the label if absent. It prints the label's name for the manager's `LABEL` parameter.

```bash
python3 -B scripts/ensure_github_label.py -- OWNER/REPO milyin-hermes 'hermes:projectmanager'
```

The label is the operator's opt-in for individual issues. Create the label once during setup and let the operator choose which issues to label.
