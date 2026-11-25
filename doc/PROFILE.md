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
          WORKER: implementer
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
            ISSUE: none
            PR: none
        - name: reviewer
          agent: codex
          model: YOUR_CODEX_MODEL
          working_directory: /absolute/path/to/review-checkout
          github_identity: milyin-hermes
          description: Review the exact PR head and report findings
          prompt_file: skills/hermes-herdr-github-project-manager/doc/REVIEWER_PROMPT.md
```

- Each worker has a unique `name`, an `agent` supported by Herdr agent manager, a `model`, an existing absolute `working_directory`, a `github_identity`, a `description`, and a `prompt_file`. Use lowercase letters, digits and hyphens in worker names.
- `manager` has the same fields except `agent`. The manager always runs Hermes and uses the configured `model`. If the manager name is omitted, setup uses the local hostname.
- `github_identity` selects the credentials used for Git and GitHub operations in that pane, including pushes and the manager's PR operations. For this deployment it is `milyin-hermes`. An unavailable identity blocks the pane launch. Credentials stay outside the prompt and the settings.
- `max_working_panes` is a positive integer. It counts worker turns that are starting, running, or may still be running. The manager pane, quota receiver and finished workers are outside that count.
- The manager's prompt file defines which GitHub repositories, issues, PRs, and wiki pages it uses and the rules it follows there. The example uses the [default manager](MANAGER_PROMPT.md), [implementer](IMPLEMENTER_PROMPT.md), and [reviewer](REVIEWER_PROMPT.md) prompts.

## Prompt files

Resolve a relative `prompt_file` path from the Hermes profile directory. Render `{{KEY}}` from the record's optional `parameters` mapping. Keys are case-sensitive identifiers made of letters, digits and underscores and must start with a letter or underscore. Substitute scalar values as text.

Manager parameters come from the manager record. All of them must resolve before the manager starts. For a worker, use the values in its `parameters` mapping as defaults. [Named parameters on `run`](COMMANDS.md#named-parameters) override those defaults for that invocation. An explicit empty value counts as supplied. A named argument may supply only a key used in the selected worker's template. Reject duplicate or unknown arguments. Report the worker and key if a referenced value is missing or invalid, before creating a pane. This lets a worker require task-specific values without making them profile setup requirements.

Validate the records and template files before starting panes. Resolve worker parameters on each run and append any literal task text after rendering. Substitute template values once. Task text, result messages, and follow-up prompts stay literal. A follow-up uses the saved worker session. The manager receives its rendered prompt and the configured worker names and descriptions on each scheduled turn, including when receiving a worker result.
