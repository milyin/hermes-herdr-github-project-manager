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

## Upgrade an installed profile

1. Identify the project's watchdog with `hermes -p PROFILE cron list`. If it is active, pause it with `hermes -p PROFILE cron pause JOB_ID` and verify that the job is listed as paused. Pausing prevents future scheduled ticks; it does not stop an admitted turn. Let in-flight work finish or reconcile it before changing a running installation.
2. Resolve the profile's skill directory and plugin link with `realpath` and verify they point to the same checkout. Check `git status --short --branch` and `git remote -v` there, not in a manager or worker checkout. Fetch the intended upstream revision using your configured Git authentication and review the changes to prompts, settings, and launchers. If the checkout is on the expected branch and can fast-forward, update it with `git merge --ff-only TARGET_SHA` and verify `git rev-parse HEAD`. Preserve local customizations. If the update overlaps local edits or the branch has diverged, reconcile the changes before proceeding; do not force-reset the installation.
3. Follow the selected workflow's migration instructions (for example, [updating a GitHub profile](../workflows/github/README.md#update-an-existing-github-profile)). Change only the settings that need migrating and read them back. For moved prompts, check that each path resolves in the selected profile and each template parameter is declared. When updating a worker list, preserve the other fields in every record and do not write masked values back to the config.
4. While the watchdog is paused, verify the installed revision, configuration, and commands required by the selected workflow. Run the [test suite](../README.md#tests) if validating an updated checkout; passing tests do not prove that the installed launchers work in the affected runtime. Do not fire the watchdog as an upgrade test. Resume with `hermes -p PROFILE cron resume JOB_ID` only when the operator is ready and prerequisites are verified; leave a deliberately paused job paused. After resuming, check the first scheduled run with `hermes -p PROFILE cron runs JOB_ID`.

## Setup interview

1. **Choose a workflow.** Present the [bundled workflows](../workflows/README.md) and the option to [create a workflow from scratch](#create-a-workflow). Confirm which workflow the operator wants before following its setup guide. For an existing setup, confirm the saved workflow and preserve the settings the operator wants to keep.
2. **Confirm the runtime settings.** Show the selected profile's `config.yaml` and its `skills.config.hermes-herdr-github-project-manager` section. Ask which Herdr instance and maximum number of working panes to use. Offer `default` and `1`. Confirm the manager and worker names, descriptions, models, existing absolute working directories, worker backends and pane tags. Confirm any supported launch profiles.
3. **Follow the selected workflow's guide.** Collect its prompt paths and parameters and run the setup checks it requires in the execution environments it uses. Show blockers to the operator before saving.
4. **Save and verify.** Confirm the configuration with the operator and update only the project-manager settings section in the selected profile's `config.yaml`. Ask the operator to run `/project-manager` to validate the settings, prepare the manager pane and check watchdog registration.

## Create a workflow

1. Choose a directory for the workflow. Write a manager prompt that defines how to find work, delegate to configured workers, handle reports and track progress. Write one prompt per worker role that describes what to do and what to report. The [workflow specification](WORKFLOW.md) defines the tools and worker event format available to every workflow.
2. Use the [local workflow profile](../workflows/hello-world/README.md#profile-settings) as a starting point for the manager and worker records. Set each `prompt_file` to your own file and declare its template parameters. Name the worker roles the manager prompt will call.
3. Configure and verify any service access in the manager or worker execution environment before enabling workflow operations. Define checks in your workflow prompts so the actors can report unavailable access.
4. Run `/project-manager` and verify a sample assignment and its result before relying on scheduled work.

## Settings

- Each worker has a unique `name`, a unique `pane_tag`, an `agent` supported by the interactive agent-manager API, a `model`, a `description`, and a `prompt_file`. The first supported worker backend is Codex. Use lowercase letters, digits and hyphens in worker names. Give each worker either an existing absolute `working_directory` or a [managed work root](#managed-worker-directories).
- `pane_tag` is a string of one to four ASCII letters or digits, such as `I`. Quote a tag that contains only digits. The engine prefixes the worker's pane status with this tag.
- `manager` has the same fields except `agent` and `pane_tag`. The manager always runs Hermes in the TUI and uses the configured `model` in its gateway session. If the manager name is omitted, setup uses the local hostname.
- `launch_profile` is an optional execution profile name. For Codex workers it selects a native Codex profile for both the app server and the TUI. Select a profile with the access your workflow requires. A manager execution profile requires Hermes to support per-session application and verification. The backend refuses a requested launch profile it cannot apply.
- `max_working_panes` is a positive integer. It counts workers that are starting, running, blocked or suspended mid-turn, possibly still running, or whose pane is locked or has an unverified lock. The manager pane, quota receiver and verified unlocked finished workers are outside that count.

## Managed worker directories

Set `managed_work_root: /absolute/path/to/worker-runs` in a worker record in place of `working_directory`. Create the root beforehand with mode `0700`, owned by the user running the manager. Use a dedicated path with no symlink components. Workers can share a root or use separate roots. Each `run` without an explicit working directory gets a fresh, empty subdirectory named for the worker and recorded privately in the profile. A configured `working_directory` or an explicit `run --cwd` remains caller-managed, including paths inside a managed root.

The manager retains every allocation, including empty directories after a pane closes. A managed `close` reports `directory_cleanup.status: retained` with reason `cleanup_guard_unavailable` when the directory can be identified. `capacity` lists the recorded paths under `managed_work`.

Automatic cleanup requires a receiver operation in `hermes-herdr-agent-manager` that verifies the exact workspace, pane, terminal, thread and completed turn, and excludes both operator and automated admissions through confirmed closure. It also requires a filesystem operation that keeps the original root and allocation identity verified through removal of the empty directory. A runtime liveness guarantee must cover every thread's working directory, root and open descriptors, and exclude new access through removal. A process leader's `/proc` entry can miss a thread with separate filesystem state, and even a complete snapshot can go stale. Ordinary pane-close receipts and legacy close records remain informational.

## Prompt files

Resolve a relative `prompt_file` path from the Hermes profile directory. Declare every `{{KEY}}` used by a prompt in that record's `parameters` mapping. Keys are case-sensitive identifiers made of letters, digits and underscores and must start with a letter or underscore. An undeclared or malformed reference is a configuration error.

Set each manager parameter to its value in the profile. For a worker, a scalar value sets the default and `null` declares a required input without a default. [Named parameters on `run`](COMMANDS.md#named-parameters) supply required values or override defaults for that run. An explicit empty string counts as supplied. The engine accepts only declared keys used in the selected worker's template. It rejects duplicate or unknown arguments and reports missing required inputs before creating the pane.

The engine resolves worker parameters on each run and substitutes their values as text in one pass. Literal task text is appended after rendering. Result messages and follow-up prompts stay literal. The manager receives its rendered prompt and the configured worker names and descriptions on each scheduled turn, including when receiving a worker result.
