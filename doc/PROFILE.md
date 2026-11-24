# Hermes project-manager profile setup

Use one Hermes profile for each project manager. The profile owns the Hermes model and tools, the three installed skills, the manager's configuration and runtime state, and its watchdog job. The [workflow specification](WORKFLOW.md) defines what the manager does with that setup.

## Select the Hermes profile

Use an existing profile reserved for project management, or create and configure one with the Hermes CLI.

```sh
hermes profile create projectmanager
hermes -p projectmanager setup
```

Select the model and credentials for the Hermes controller during setup. Its model is separate from the Codex model that the project manager assigns to workers. The controller needs local terminal access to the Herdr session and skill executables. Use the same profile for the controller, operator commands and cron. See [Hermes profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles).

Resolve the profile directory from Hermes before running the standalone Python manager. Use the actual profile name in place of `projectmanager`.

```sh
profile_name=projectmanager
profile_config=$(hermes -p "$profile_name" config path) || exit 1
export HERMES_HOME="$(dirname "$profile_config")"
project_dir="$HERMES_HOME/github-project-manager"
manager_skill="$HERMES_HOME/skills/hermes-herdr-github-project-manager"
```

## Install skills in the selected profile

Install Linux-compatible versions of Hermes, Herdr, Git, `gh`, Codex and Python 3.10 or later with PyYAML. Clone each repository into the corresponding directory under `$HERMES_HOME/skills/`. Use the complete Git checkouts. The manager verifies dependency entry points and their checkout identities.

| Skill directory | Repository |
| --- | --- |
| `hermes-herdr-pane-manager` | [Herdr pane manager](https://github.com/milyin/hermes-herdr-pane-manager) |
| `hermes-herdr-agent-manager` | [Herdr agent manager](https://github.com/milyin/hermes-herdr-agent-manager) |
| `hermes-herdr-github-project-manager` | [GitHub project manager](https://github.com/milyin/hermes-herdr-github-project-manager) |

For a new installation, the following leaves existing directories alone. Check an existing installation's origin and revision before using it.

```sh
mkdir -p "$HERMES_HOME/skills"
for skill_name in hermes-herdr-pane-manager hermes-herdr-agent-manager hermes-herdr-github-project-manager; do
  skill_dir="$HERMES_HOME/skills/$skill_name"
  if [ ! -e "$skill_dir" ] && [ ! -L "$skill_dir" ]; then
    git clone "https://github.com/milyin/$skill_name.git" "$skill_dir" || exit 1
  fi
done
```

## Create the project configuration

Keep the manager's configuration and runtime files in `$HERMES_HOME/github-project-manager/`. Use an owner-only directory and an owner-controlled regular YAML file. The current manager defaults to `config.yaml` inside that directory.

```sh
install -d -m 700 "$project_dir"
project_config="$project_dir/config.yaml"
if [ ! -e "$project_config" ] && [ ! -L "$project_config" ]; then
  install -m 600 "$manager_skill/examples/project.yaml" "$project_config"
fi
```

Start from the [example for the current implementation](../examples/project.yaml). Fill in the deployment values before running preflight.

| Project setting | What to configure |
| --- | --- |
| Manager name | A public-safe name unique across managers that share issue routing or a dashboard wiki. |
| Herdr | An already running session and the exact ID of a dedicated workspace in that session. A workspace label can help locate it. Use the pane manager's returned ID in the configuration. |
| Worker | The Codex model, capacity per manager and per pass, quota receiver if already provisioned, and network access for assignments that use GitHub. |
| GitHub | The GitHub App account assigned to workers and the allowlisted targets. Record each target's full name, numeric repository ID, base branch, and whether it is owned or external. Configure App access separately so credentials stay out of prompts. The [worker access design](WORKFLOW.md#worker-github-access) describes the planned account boundary. |
| Dashboard | The destination repository, numeric repository ID, and identity. Initialize the dashboard wiki and any owned repository wikis required by the current preflight. |
| Limits and maintenance | Pass interval, host health and retention limits, the repository for each of the three skill updates, and the required workflow checks for those updates. |

In the current example, define the manager's GitHub identities under `publication.identities` and select one for the dashboard and for each repository. For an App identity, set its expected bot login and the absolute path to the configured launcher.

To discover workspace IDs for the default Herdr session, run the pane manager's `workspaces` command. Substitute the configured session name when it differs.

```sh
python3 -I -B "$HERMES_HOME/skills/hermes-herdr-pane-manager/scripts/pane_manager.py" workspaces --instance default
```

## Link the project file from the Hermes profile

Hermes supports non-secret skill settings under `skills.config`. Use that namespace for an optional path setting in the profile's own `config.yaml`. The example below selects an alternate project file. The default file needs no setting. See [Hermes skill configuration](https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills#config-settings-configyaml).

```yaml
skills:
  config:
    hermes-herdr-github-project-manager:
      config_path: github-project-manager/team.yaml
```

This path setting is proposed. The project manager needs to resolve it itself so direct commands, the supervisor and the no-agent watchdog all use the same value. File selection should use `--config` first, then the profile setting, then `github-project-manager/config.yaml`. Resolve a relative path from the selected profile's home. Require a regular owner-controlled file directly inside that profile's `github-project-manager/` directory. An explicit invalid path blocks preflight. The setting selects a separate YAML file. If the selected path changes, repeat preflight and activation and refresh the watchdog configuration.

The current manager supports the default path and an explicit `--config` argument. Until it reads the profile setting, use the absolute project file path when calling it. Set `project_config="$project_dir/team.yaml"` to use the alternate file from the example. Use the same file at preflight, activation and watchdog installation.

```sh
python3 -B "$manager_skill/scripts/manager.py" --config "$project_config" check
python3 -B "$manager_skill/scripts/manager.py" --config "$project_config" preview
```

Continue with the [startup sequence](WORKFLOW.md#profile-startup-and-periodic-passes) when preflight passes. Register the watchdog under the selected Hermes profile.
