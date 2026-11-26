# Project-manager profile

## Install and open the profile

Install the project-manager, pane-manager, and agent-manager checkouts in the selected profile's `skills/` directory. Link the project-manager checkout from the profile's `plugins/hermes-herdr-github-project-manager` so Hermes loads its command, tools and startup hook from the same checkout. Enable that plugin in the profile settings below. The engine uses the installed Hermes gateway and terminal UI on Linux.

Set `enabled: true` under `skills.config.hermes-herdr-github-project-manager` in the profile's `config.yaml`, then open it with `hermes -p projectmanager`. If project-manager settings are missing or invalid, the operator conversation starts the [setup interview](#setup-interview). A missing flag or `enabled: false` skips startup. The managed TUI uses its prepared binding and skips setup.

After validating the settings, the integration starts or verifies the selected profile's Hermes gateway and scheduler and registers one no-agent watchdog for that profile. It immediately runs pane preparation to open the [manager conversation in a Hermes TUI pane](WORKFLOW.md#manager-conversation). Opening the profile again reuses the verified workspace, manager conversation, pane, and cron job. The gateway and scheduler keep running after the setup session ends.

Use `/project-manager` or Hermes cron history to verify the watchdog. The watchdog runs once a minute. Report scheduler verification as pending until it records a successful scheduled run. Pane preparation reports the manager's attachment separately. A scheduled tick that exits normally without work confirms the watchdog ran. Report an overdue or failed first scheduled run with the scheduler's reason when available. [Hermes cron scheduler](https://hermes-agent.nousresearch.com/docs/guides/cron-troubleshooting#check-3-is-the-gateway-running)

## Setup interview

1. **Choose the workflow.** Ask whether to install the bundled GitHub workflow or the local hello-world demo. Offer GitHub as the default. The demo exercises manager-to-worker delegation and result delivery without a repository, GitHub App, wiki, or issue label. If the operator already uses a custom workflow, confirm that choice and collect its prompt files and parameters.
2. **Confirm the runtime settings.** Show the selected profile's `config.yaml` path and the `skills.config.hermes-herdr-github-project-manager` section. Ask which Herdr instance and maximum number of working panes to use. Offer `default` and `1`. Confirm the manager and worker names, descriptions, models and existing absolute working directories. Confirm the worker roles, pane tags and any Codex launch profiles needed to run them. Offer the roles and tags in the examples below. For an existing configuration, confirm its saved values and preserve settings the operator wants to keep.
3. **Select the GitHub repository and App.** For the GitHub workflow, ask for one `OWNER/REPO` and the GitHub App slug. Set the same `github_identity` in the manager and both worker defaults. Show the operator the [GitHub App setup guide](https://github.com/milyin/hermes-herdr-github-project-manager/blob/main/doc/GITHUB_APP.md). Ask the operator to complete the helper setup and verify it with `APP_SLUG --repo OWNER/REPO status`. Confirm the reported actor and repository. The helper must also work in the manager and workers' execution environments. Keep credentials in the helper's private configuration.
4. **Check the wiki.** Explain that the bundled GitHub workflow publishes its dashboard to the repository wiki. On GitHub.com, a private repository needs a paid plan that includes wikis, such as GitHub Pro for a personal repository or GitHub Team or Enterprise Cloud for an organization. Ask the operator to confirm that the wiki is available, enabled, and initialized with a first page. Verify App access with `APP_SLUG --repo OWNER/REPO git ls-remote https://github.com/OWNER/REPO.wiki.git`. Report an access failure as a setup blocker and offer the demo if the operator cannot use the wiki. [GitHub wiki availability](https://docs.github.com/en/communities/documenting-your-project-with-wikis)
5. **Agree on the opt-in label.** Ask which label should opt an issue into automation. Offer `hermes:projectmanager`. Explain that the manager will select open issues with this label, and that setup will create it if absent. After the operator confirms the target repository, App, and label, run the bundled `scripts/ensure_github_label.py` from the installed checkout using the [label setup command](GITHUB_APP.md#opt-in-label). Save the label name returned by the script in the manager's `LABEL` parameter. If the App cannot verify or create the label, report the blocker and hold GitHub setup. The operator can apply the label to issues when they want work to start.
6. **Save and verify.** Confirm the selected workflow and settings with the operator, then update only the project-manager settings section in the selected profile's `config.yaml`. Point out that this file is where they can change models, directories, pane limits and workflow parameters later. Ask them to run `/project-manager` to validate the settings, prepare the manager pane and check watchdog registration. For the demo, the first successful workflow run leaves a `hello` worker pane and reports its greeting in the manager conversation.

## Settings

Store the project-manager configuration in the Hermes profile under `skills.config.hermes-herdr-github-project-manager`.

This example configures the GitHub workflow with its [manager](MANAGER_PROMPT.md), [implementer](IMPLEMENTER_PROMPT.md), and [reviewer](REVIEWER_PROMPT.md) prompts. Replace the repository, models, and working directories with your own values. To use a different workflow, choose its prompt files and supply their template parameters.

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
        prompt_file: skills/hermes-herdr-github-project-manager/doc/MANAGER_PROMPT.md
        parameters:
          REPO: owner/repository
          LABEL: hermes:projectmanager
          IMPLEMENTER: implementer
          REVIEWER: reviewer
          github_identity: milyin-hermes
      workers:
        - name: implementer
          pane_tag: I
          agent: codex
          model: YOUR_CODEX_MODEL
          working_directory: /absolute/path/to/implementer-checkout
          description: Implement issues and address PR feedback
          prompt_file: skills/hermes-herdr-github-project-manager/doc/IMPLEMENTER_PROMPT.md
          parameters:
            REPO: null
            ISSUE: none
            PR: none
            TASK: null
            github_identity: milyin-hermes
        - name: reviewer
          pane_tag: R
          agent: codex
          model: YOUR_CODEX_MODEL
          working_directory: /absolute/path/to/reviewer-checkout
          description: Review the exact PR head and report findings
          prompt_file: skills/hermes-herdr-github-project-manager/doc/REVIEWER_PROMPT.md
          parameters:
            REPO: null
            ISSUE: null
            PR: null
            HEAD: null
            github_identity: milyin-hermes
```

For this example, `github_identity` is both the App slug and the name of its scoped helper on `PATH`. The manager passes its selected identity to both workers on assignment. Keep the worker defaults in sync for manual runs. See [GitHub App setup](GITHUB_APP.md).

- Each worker has a unique `name`, a unique `pane_tag`, an `agent` supported by the interactive agent-manager API, a `model`, an existing absolute `working_directory`, a `description`, and a `prompt_file`. The first supported worker backend is `codex`. Use lowercase letters, digits and hyphens in worker names. The working directory is the default for `run`. A workflow can supply a separate directory for each pane it retains.
- `pane_tag` is a string of one to four ASCII letters or digits, such as `I` for implementer and `R` for reviewer. Quote a tag that contains only digits. The engine prefixes the worker's pane status with this tag.
- `manager` has the same fields except `agent` and `pane_tag`. The manager always runs Hermes in the TUI and uses the configured `model` in its gateway session. If the manager name is omitted, setup uses the local hostname.
- `launch_profile` is an optional execution profile name. For Codex workers it selects a native Codex profile for both the app server and the TUI. Select a profile with the tool and network access the workflow requires. A manager execution profile requires Hermes to support per-session application and verification. Refuse a requested launch profile when its backend cannot apply it.
- `max_working_panes` is a positive integer. It counts workers that are starting, running, blocked or suspended mid-turn, possibly still running, or whose pane is locked or has an unverified lock. The manager pane, quota receiver and verified unlocked finished workers are outside that count.

### Hello-world demo

Use the same plugin registration as the GitHub example. In the project-manager settings section, use these manager and worker records with your models and directories. The demo manager expects a worker named `hello`.

```yaml
enabled: true
herdr:
  instance: default
max_working_panes: 1
manager:
  name: projectmanager
  model: YOUR_HERMES_MODEL
  working_directory: /absolute/path/to/manager
  description: Demonstrate local worker delegation
  prompt_file: skills/hermes-herdr-github-project-manager/doc/DEMO_MANAGER_PROMPT.md
workers:
  - name: hello
    pane_tag: H
    agent: codex
    model: YOUR_CODEX_MODEL
    working_directory: /absolute/path/to/worker
    description: Send a demo greeting
    prompt_file: skills/hermes-herdr-github-project-manager/doc/DEMO_WORKER_PROMPT.md
```

## Prompt files

Resolve a relative `prompt_file` path from the Hermes profile directory. Declare every `{{KEY}}` used by a prompt in that record's `parameters` mapping. Keys are case-sensitive identifiers made of letters, digits and underscores and must start with a letter or underscore. Validate templates against their declarations before opening panes or using changed settings. An undeclared or malformed reference is a configuration error. Report the record, prompt file and reference.

Set each manager parameter to its value in the profile. For a worker, a scalar value sets the default and `null` declares a required input without a default. [Named parameters on `run`](COMMANDS.md#named-parameters) supply required values or override defaults for that run. An explicit empty string counts as supplied. Accept only declared keys used in the selected worker's template. Reject duplicate or unknown arguments. If a required input has no value, report the worker and key before creating its pane.

The engine resolves worker parameters on each run and substitutes their values as text in one pass. Append any literal task text after rendering. Result messages and follow-up prompts stay literal. A follow-up uses the bound worker session. The manager receives its rendered prompt and the configured worker names and descriptions on each scheduled turn, including when receiving a worker result.
