# Project-manager profile

## Install and open the profile

Install the project-manager, pane-manager, and agent-manager checkouts in the selected profile's `skills/` directory. Link the project-manager checkout from the profile's `plugins/hermes-herdr-github-project-manager` so Hermes loads its command, tools and startup hook from the same checkout. Enable that plugin in the profile settings below. The engine uses the installed Hermes gateway and terminal UI on Linux.

Set `enabled: true` and open the profile with `hermes -p projectmanager`. The operator conversation starts the setup interview if required settings are missing. It asks for the affected settings from this document. A missing flag or `enabled: false` skips startup. The managed TUI uses its prepared binding and skips setup.

After validating the settings, the integration starts or verifies the selected profile's Hermes gateway and scheduler and registers one no-agent watchdog for that profile. It immediately runs pane preparation to open the [manager conversation in a Hermes TUI pane](WORKFLOW.md#manager-conversation). Opening the profile again reuses the verified workspace, manager conversation, pane, and cron job. The gateway and scheduler keep running after the setup session ends.

Use `/project-manager` or Hermes cron history to verify the watchdog. The watchdog runs once a minute. Report scheduler verification as pending until it records a successful scheduled run. Pane preparation reports the manager's attachment separately. A scheduled tick that exits normally without work confirms the watchdog ran. Report an overdue or failed first scheduled run with the scheduler's reason when available. [Hermes cron scheduler](https://hermes-agent.nousresearch.com/docs/guides/cron-troubleshooting#check-3-is-the-gateway-running)

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

For this example, `github_identity` tells the manager and workers to use the `milyin-hermes` App account. It is a prompt parameter. The workflow reports authentication or push failures as blockers for the operator.

- Each worker has a unique `name`, a unique `pane_tag`, an `agent` supported by the interactive agent-manager API, a `model`, an existing absolute `working_directory`, a `description`, and a `prompt_file`. The first supported worker backend is `codex`. Use lowercase letters, digits and hyphens in worker names. The working directory is the default for `run`. A workflow can supply a separate directory for each pane it retains.
- `pane_tag` is a string of one to four ASCII letters or digits, such as `I` for implementer and `R` for reviewer. Quote a tag that contains only digits. The engine prefixes the worker's pane status with this tag.
- `manager` has the same fields except `agent` and `pane_tag`. The manager always runs Hermes in the TUI and uses the configured `model` in its gateway session. If the manager name is omitted, setup uses the local hostname.
- `launch_profile` is an optional execution profile name. For Codex workers it selects a native Codex profile for both the app server and the TUI. Select a profile with the tool and network access the workflow requires. A manager execution profile requires Hermes to support per-session application and verification. Refuse a requested launch profile when its backend cannot apply it.
- `max_working_panes` is a positive integer. It counts workers that are starting, running, blocked or suspended mid-turn, possibly still running, or whose pane is locked or has an unverified lock. The manager pane, quota receiver and verified unlocked finished workers are outside that count.

## Prompt files

Resolve a relative `prompt_file` path from the Hermes profile directory. Declare every `{{KEY}}` used by a prompt in that record's `parameters` mapping. Keys are case-sensitive identifiers made of letters, digits and underscores and must start with a letter or underscore. Validate templates against their declarations before opening panes or using changed settings. An undeclared or malformed reference is a configuration error. Report the record, prompt file and reference.

Set each manager parameter to its value in the profile. For a worker, a scalar value sets the default and `null` declares a required input without a default. [Named parameters on `run`](COMMANDS.md#named-parameters) supply required values or override defaults for that run. An explicit empty string counts as supplied. Accept only declared keys used in the selected worker's template. Reject duplicate or unknown arguments. If a required input has no value, report the worker and key before creating its pane.

The engine resolves worker parameters on each run and substitutes their values as text in one pass. Append any literal task text after rendering. Result messages and follow-up prompts stay literal. A follow-up uses the bound worker session. The manager receives its rendered prompt and the configured worker names and descriptions on each scheduled turn, including when receiving a worker result.
