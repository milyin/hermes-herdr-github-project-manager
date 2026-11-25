# Project-manager profile

Open the configured profile with `hermes -p projectmanager`. The startup integration validates the settings, enables one no-agent cron watchdog for the profile, and runs it immediately to prepare the [manager workspace and panes](WORKFLOW.md#pane-lifecycle). An enabled profile with missing settings starts the setup interview. Opening the profile again reuses the same verified workspace, manager pane and cron job.

## Settings

Store the project-manager configuration in the Hermes profile under `skills.config.hermes-herdr-github-project-manager`.

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
        github_identity: milyin-hermes
        description: Manage the repository and its dashboard
        prompt_file: skills/hermes-herdr-github-project-manager/doc/MANAGER_PROMPT.md
        parameters:
          REPO: owner/repository
          LABEL: hermes:projectmanager
          IMPLEMENTER: implementer
          REVIEWER: reviewer
      workers:
        - name: implementer
          agent: codex
          model: YOUR_CODEX_MODEL
          working_directory: /absolute/path/to/worker-checkout
          github_identity: milyin-hermes
          description: Implement issues and address PR feedback
          prompt_file: skills/hermes-herdr-github-project-manager/doc/IMPLEMENTER_PROMPT.md
          parameters:
            REPO: null
            ISSUE: none
            PR: none
            TASK: null
        - name: reviewer
          agent: codex
          model: YOUR_CODEX_MODEL
          working_directory: /absolute/path/to/review-checkout
          github_identity: milyin-hermes
          description: Review the exact PR head and report findings
          prompt_file: skills/hermes-herdr-github-project-manager/doc/REVIEWER_PROMPT.md
          parameters:
            REPO: null
            ISSUE: null
            PR: null
            HEAD: null
```

- Each worker has a unique `name`, an `agent` supported by Herdr agent manager, a `model`, an existing absolute `working_directory`, a `github_identity`, a `description`, and a `prompt_file`. Use lowercase letters, digits and hyphens in worker names.
- `manager` has the same fields except `agent`. The manager always runs Hermes and uses the configured `model`. If the manager name is omitted, setup uses the local hostname.
- `github_identity` selects the credentials used for Git and GitHub operations in that pane, including pushes and the manager's PR operations. For this deployment it is `milyin-hermes`. An unavailable identity blocks the pane launch. Credentials stay outside the prompt and the settings.
- `max_working_panes` is a positive integer. It counts workers that are starting, running, possibly still running, or whose pane is locked or has an unverified lock. The manager pane, quota receiver and verified unlocked finished workers are outside that count.
- The manager's prompt file defines which GitHub repositories, issues, PRs, and wiki pages it uses and the rules it follows there. The example uses the [default manager](MANAGER_PROMPT.md), [implementer](IMPLEMENTER_PROMPT.md), and [reviewer](REVIEWER_PROMPT.md) prompts.

## Prompt files

Resolve a relative `prompt_file` path from the Hermes profile directory. Declare every `{{KEY}}` used by a prompt in that record's `parameters` mapping. Keys are case-sensitive identifiers made of letters, digits and underscores and must start with a letter or underscore. Validate templates against their declarations before opening panes or using changed settings. An undeclared or malformed reference is a configuration error. Report the record, prompt file and reference.

Set each manager parameter to its value in the profile. For a worker, a scalar value sets the default and `null` declares a required input without a default. [Named parameters on `run`](COMMANDS.md#named-parameters) supply required values or override defaults for that run. An explicit empty string counts as supplied. Accept only declared keys used in the selected worker's template. Reject duplicate or unknown arguments. If a required input has no value, report the worker and key before creating its pane.

Resolve worker parameters on each run and substitute their values as text in one pass. Append any literal task text after rendering. Result messages and follow-up prompts stay literal. A follow-up uses the saved worker session. The manager receives its rendered prompt and the configured worker names and descriptions on each scheduled turn, including when receiving a worker result.
