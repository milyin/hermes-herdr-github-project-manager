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
        launch_profile: manager-profile
        description: Coordinate work and handle results
        prompt_file: prompts/manager.md
        parameters:
          WORKER: summarizer
      workers:
        - name: summarizer
          agent: codex
          model: YOUR_CODEX_MODEL
          working_directory: /absolute/path/to/worker
          launch_profile: worker-profile
          description: Summarize supplied material
          prompt_file: prompts/summarizer.md
          parameters:
            TASK: null
            FORMAT: bullets
```

- Each worker has a unique `name`, an `agent` supported by Herdr agent manager, a `model`, an existing absolute `working_directory`, a `description`, and a `prompt_file`. Use lowercase letters, digits and hyphens in worker names.
- `manager` has the same fields except `agent`. The manager always runs Hermes and uses the configured `model`. If the manager name is omitted, setup uses the local hostname.
- `launch_profile` is an optional name for a per-pane execution profile supplied by the agent manager. The runtime passes through the name. The agent manager must apply the requested environment and credentials or refuse the launch. Store credentials in the agent manager's credential setup.
- `max_working_panes` is a positive integer. It counts workers that are starting, running, blocked or suspended mid-turn, possibly still running, or whose pane is locked or has an unverified lock. The manager pane, quota receiver and verified unlocked finished workers are outside that count.
- Select prompts for your project. The [example manager](MANAGER_PROMPT.md), [implementer](IMPLEMENTER_PROMPT.md), and [reviewer](REVIEWER_PROMPT.md) prompts define one workflow. Supply their template parameters when selecting them.

## Prompt files

Resolve a relative `prompt_file` path from the Hermes profile directory. Declare every `{{KEY}}` used by a prompt in that record's `parameters` mapping. Keys are case-sensitive identifiers made of letters, digits and underscores and must start with a letter or underscore. Validate templates against their declarations before opening panes or using changed settings. An undeclared or malformed reference is a configuration error. Report the record, prompt file and reference.

Set each manager parameter to its value in the profile. For a worker, a scalar value sets the default and `null` declares a required input without a default. [Named parameters on `run`](COMMANDS.md#named-parameters) supply required values or override defaults for that run. An explicit empty string counts as supplied. Accept only declared keys used in the selected worker's template. Reject duplicate or unknown arguments. If a required input has no value, report the worker and key before creating its pane.

Resolve worker parameters on each run and substitute their values as text in one pass. Append any literal task text after rendering. Result messages and follow-up prompts stay literal. A follow-up uses the saved worker session. The manager receives its rendered prompt and the configured worker names and descriptions on each scheduled turn, including when receiving a worker result.
