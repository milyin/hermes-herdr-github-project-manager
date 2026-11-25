# Project-manager profile

Open the configured profile with `hermes -p projectmanager`. An enabled profile with missing settings starts the setup interview. The startup integration validates the settings, ensures the selected profile's Hermes gateway and scheduler are running, and enables one no-agent cron watchdog for the profile. It runs the watchdog immediately to prepare the workspace and open the [manager conversation in a Hermes TUI pane](WORKFLOW.md#manager-conversation). The managed TUI uses the prepared binding and skips the setup interview.

Setup is ready when the manager pane is attached to its gateway conversation and the profile's scheduler is active. Opening the profile again reuses the verified workspace, manager conversation, pane, and cron job. The gateway and scheduler keep running after the setup session ends. [Hermes cron scheduler](https://hermes-agent.nousresearch.com/docs/guides/cron-troubleshooting#check-3-is-the-gateway-running)

## Settings

Store the project-manager configuration in the Hermes profile under `skills.config.hermes-herdr-github-project-manager`.

This example configures the [GitHub manager](MANAGER_PROMPT.md), [implementer](IMPLEMENTER_PROMPT.md), and [reviewer](REVIEWER_PROMPT.md) prompts. Replace the repository, models, and working directories with your own values.

```yaml
skills:
  config:
    hermes-herdr-github-project-manager:
      herdr:
        instance: default
      max_working_panes: 1
      manager:
        name: projectmanager
        model: YOUR_HERMES_MODEL
        working_directory: /absolute/path/to/manager
        launch_profile: milyin-hermes
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
          agent: codex
          model: YOUR_CODEX_MODEL
          working_directory: /absolute/path/to/implementer-checkout
          launch_profile: milyin-hermes
          description: Implement issues and address PR feedback
          prompt_file: skills/hermes-herdr-github-project-manager/doc/IMPLEMENTER_PROMPT.md
          parameters:
            REPO: null
            ISSUE: none
            PR: none
            TASK: null
            github_identity: milyin-hermes
        - name: reviewer
          agent: codex
          model: YOUR_CODEX_MODEL
          working_directory: /absolute/path/to/reviewer-checkout
          launch_profile: milyin-hermes
          description: Review the exact PR head and report findings
          prompt_file: skills/hermes-herdr-github-project-manager/doc/REVIEWER_PROMPT.md
          parameters:
            REPO: null
            ISSUE: null
            PR: null
            HEAD: null
            github_identity: milyin-hermes
```

For this example, configure the `milyin-hermes` launch profile with the App account credentials. The manager uses that profile in its gateway session and the workers use it in their agent-manager sessions. Each prompt's `github_identity` parameter names that account.

- Each worker has a unique `name`, an `agent` supported by Herdr agent manager, a `model`, an existing absolute `working_directory`, a `description`, and a `prompt_file`. Use lowercase letters, digits and hyphens in worker names.
- `manager` has the same fields except `agent`. The manager always runs Hermes in the TUI and uses the configured `model` in its gateway session. If the manager name is omitted, setup uses the local hostname.
- `launch_profile` is an optional execution profile name. The agent manager applies it to workers, and the gateway integration applies it to the manager session's tool processes. Refuse the launch when the requested environment or credentials cannot be applied. Store credentials in the launch profile's credential setup.
- `max_working_panes` is a positive integer. It counts workers that are starting, running, blocked or suspended mid-turn, possibly still running, or whose pane is locked or has an unverified lock. The manager pane, quota receiver and verified unlocked finished workers are outside that count.

## Prompt files

Resolve a relative `prompt_file` path from the Hermes profile directory. Declare every `{{KEY}}` used by a prompt in that record's `parameters` mapping. Keys are case-sensitive identifiers made of letters, digits and underscores and must start with a letter or underscore. Validate templates against their declarations before opening panes or using changed settings. An undeclared or malformed reference is a configuration error. Report the record, prompt file and reference.

Set each manager parameter to its value in the profile. For a worker, a scalar value sets the default and `null` declares a required input without a default. [Named parameters on `run`](COMMANDS.md#named-parameters) supply required values or override defaults for that run. An explicit empty string counts as supplied. Accept only declared keys used in the selected worker's template. Reject duplicate or unknown arguments. If a required input has no value, report the worker and key before creating its pane.

Resolve worker parameters on each run and substitute their values as text in one pass. Append any literal task text after rendering. Result messages and follow-up prompts stay literal. A follow-up uses the saved worker session. The manager receives its rendered prompt and the configured worker names and descriptions on each scheduled turn, including when receiving a worker result.
