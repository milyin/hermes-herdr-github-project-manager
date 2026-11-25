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
      workers:
        - name: implementer
          agent: codex
          model: YOUR_CODEX_MODEL
          working_directory: /absolute/path/to/worker-checkout
          github_identity: milyin-hermes
          description: Implement issues and address PR feedback
          prompt_file: prompts/implementer.md
```

- Each worker has a unique `name`, an `agent` supported by Herdr agent manager, a `model`, an existing absolute `working_directory`, a `github_identity`, a `description`, and a `prompt_file`. Use lowercase letters, digits and hyphens in worker names.
- `manager` has the same fields except `agent`. The manager always runs Hermes and uses the configured `model`. If the manager name is omitted, setup uses the local hostname.
- `github_identity` selects the credentials used for Git and GitHub operations in that pane, including pushes and the manager's PR operations. For this deployment it is `milyin-hermes`. An unavailable identity blocks the pane launch. Credentials stay outside the prompt and the settings.
- `max_working_panes` is a positive integer. It counts worker turns that are starting, running, or may still be running. The manager pane, quota receiver and finished workers are outside that count.
- The manager's prompt file defines which GitHub repositories, issues, PRs, and wiki pages it uses and the rules it follows there. The example uses the [default manager prompt](MANAGER_PROMPT.md).

## Prompt files

Resolve a relative `prompt_file` path from the Hermes profile directory. Render `{{KEY}}` from the optional `parameters` mapping on that same manager or worker record. Keys are case-sensitive identifiers made of letters, digits and underscores and must start with a letter or underscore. Substitute scalar values as text. Report the record and key if a referenced value is missing or invalid. Validate all records and prompt files before starting panes, and validate changed settings before using them. Substitute template values once. Treat task prompts and replies as literal text.

The manager receives the rendered manager prompt and the configured worker names and descriptions on each scheduled turn, including when receiving a worker result. A new worker turn combines the rendered worker prompt with the task. Follow-ups use the saved worker session.
