# Project-manager profile

## Install and open the profile

Install the project-manager, pane-manager, and agent-manager checkouts in the selected profile's `skills/` directory. Link the project-manager checkout from the profile's `plugins/hermes-herdr-github-project-manager` so Hermes loads its command, tools and startup hook from the same checkout.

Enable the plugin and the skill in the profile's `config.yaml`.

```yaml
plugins:
  enabled:
    - hermes-herdr-github-project-manager

skills:
  config:
    hermes-herdr-github-project-manager:
      enabled: true
```

Open the profile with `hermes -p PROFILE`. If project-manager settings are missing or invalid, the operator conversation starts the setup interview. A missing flag or `enabled: false` skips startup. The managed TUI uses its prepared binding and skips setup.

After validating the settings, the integration starts or verifies the selected profile's Hermes gateway and scheduler and registers one no-agent watchdog for that profile. It runs pane preparation to open the [manager conversation in a Hermes TUI pane](WORKFLOW.md#manager-conversation). Opening the profile again reuses the verified workspace, manager conversation, pane, and cron job. The gateway and scheduler keep running after the setup session ends.

Use `/project-manager` or Hermes cron history to verify the watchdog. A successful scheduled tick, including a tick with no work, confirms the watchdog ran. Report scheduler verification as pending until that first scheduled run, and report overdue or failed runs with the scheduler's reason when available. Pane preparation reports the manager's attachment separately. [Hermes cron scheduler](https://hermes-agent.nousresearch.com/docs/guides/cron-troubleshooting#check-3-is-the-gateway-running)

## Setup interview

1. **Choose a workflow.** Present the [bundled workflows](../workflows/README.md) and the option to [create a workflow from scratch](#create-a-workflow). Confirm which workflow the operator wants before following its setup guide. For an existing setup, confirm the saved workflow and preserve the settings the operator wants to keep.
2. **Confirm the runtime settings.** Show the selected profile's `config.yaml` and its `skills.config.hermes-herdr-github-project-manager` section. Ask which Herdr instance and maximum number of working panes to use. Offer `default` and `1`. Confirm the manager and worker names, descriptions, models, existing absolute working directories, worker backends and pane tags. Confirm any supported launch profiles.
3. **Follow the selected workflow's guide.** Collect its prompt paths and parameters and run the setup checks it requires in the execution environments it uses. Show blockers to the operator before saving.
4. **Save and verify.** Confirm the configuration with the operator and update only the project-manager settings section in the selected profile's `config.yaml`. Ask the operator to run `/project-manager` to validate the settings, prepare the manager pane and check watchdog registration.

## Create a workflow

1. Choose a directory for the workflow. Write a manager prompt that defines how to find work, delegate to configured workers, handle reports and track progress. Write one prompt per worker role that describes what to do and what to report. The [workflow specification](WORKFLOW.md) defines the tools and worker event format available to every workflow.
2. Use the [local workflow profile](../workflows/hello-world/README.md#profile-settings) as a starting point for the manager and worker records. Set each `prompt_file` to your own file and declare its template parameters. Name the worker roles the manager prompt will call.
3. Configure and verify any service access before enabling workflow operations. Use an [environment setup hook](#environment-setup-hooks) to prepare or check what each managed turn needs.
4. Run `/project-manager` and verify a sample assignment and its result before relying on scheduled work.

## Settings

- Each worker has a unique `name`, a unique `pane_tag`, an `agent` supported by the interactive agent-manager API, a `model`, an existing absolute `working_directory`, a `description`, and a `prompt_file`. The first supported worker backend is Codex. Use lowercase letters, digits and hyphens in worker names. The working directory is the default for `run`. A workflow can supply a separate directory for each pane it retains.
- `pane_tag` is a string of one to four ASCII letters or digits, such as `I`. Quote a tag that contains only digits. The engine prefixes the worker's pane status with this tag.
- `manager` has the same fields except `agent` and `pane_tag`. The manager always runs Hermes in the TUI and uses the configured `model` in its gateway session. If the manager name is omitted, setup uses the local hostname.
- `launch_profile` is an optional execution profile name. For Codex workers it selects a native Codex profile for both the app server and the TUI. Select a profile with the access your workflow requires. A manager execution profile requires Hermes to support per-session application and verification. The backend refuses a requested launch profile it cannot apply.
- `max_working_panes` is a positive integer. It counts workers that are starting, running, blocked or suspended mid-turn, possibly still running, or whose pane is locked or has an unverified lock. The manager pane, quota receiver and verified unlocked finished workers are outside that count.

## Environment setup hooks

Set `setup_hook` in the project-manager configuration to register a trusted Python script for the selected workflow. Relative paths resolve from the Hermes profile. The bound execution runtime runs the hook in the agent's working directory before a managed manager turn, the first worker turn, or a worker follow-up. The engine sends a JSON object with `agent`, `backend`, `manager` (a boolean), `working_directory`, and `parameters` on standard input.

On success, write a single JSON object to standard output and exit zero. A prompt may reference hook output using `{{setup_NAME}}` placeholders. The `setup_` prefix is reserved for hook output. Return exactly those fields as strings, or return `{}` for a prompt with no hook values. Use non-secret values such as a verified command path. The engine records these values with the worker's run and checks them before a follow-up. Start a new worker run if they change.

To block a turn, exit nonzero and return an `error` string in a JSON object. The engine reports it to the caller. Keep the hook safe to rerun and finish within 35 seconds.

Hook-enabled workflows require a Hermes gateway and agent manager that support [bound environment verification](WORKFLOW.md#bound-environment-verification). If either runtime cannot verify its actual turn environment, the engine holds that turn. The worker pane attaches before its first check so the runtime can verify its working directory, launch profile and execution policy.

## Prompt files

Resolve a relative `prompt_file` path from the Hermes profile directory. Declare each `{{KEY}}` in that record's `parameters` mapping or supply it from the setup hook as described above. Keys are case-sensitive identifiers made of letters, digits and underscores and must start with a letter or underscore. An undeclared or malformed reference is a configuration error.

Set each manager parameter to its value in the profile. For a worker, a scalar value sets the default and `null` declares a required input without a default. [Named parameters on `run`](COMMANDS.md#named-parameters) supply required values or override defaults for that run. An explicit empty string counts as supplied. The engine accepts only declared keys used in the selected worker's template. It rejects duplicate or unknown arguments and reports missing required inputs before creating the pane.

The engine resolves worker parameters on each run and substitutes their values as text in one pass. Literal task text is appended after rendering. Result messages and follow-up prompts stay literal. The manager receives its rendered prompt and the configured worker names and descriptions on each scheduled turn, including when receiving a worker result.
