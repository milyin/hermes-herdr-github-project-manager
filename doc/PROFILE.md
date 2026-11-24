# Hermes project-manager profile setup

This guide describes the intended first run. The Hermes profile is the source of project settings and keeps the installed skills, saved task state, and watchdog job together.

## First run

Open a profile enabled for the project-manager role.

```sh
hermes -p projectmanager
```

If the profile has the required project settings, startup runs the [startup sequence](WORKFLOW.md#profile-startup-and-periodic-passes) automatically, before the first chat message. It prepares the Herdr workspace, installs and enables the watchdog job, then runs the watchdog immediately to launch the manager. If project settings are missing, startup begins the [setup interview](#setup-interview). See [Hermes profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles).

Setup reports ready once the manager pane has a fresh apply heartbeat and Hermes confirms an enabled watchdog job with a successful run and a scheduler that will continue after the opening session exits. The readiness report identifies the workspace, manager pane, and watchdog job. Reopening the profile reuses the verified workspace and job. If the Herdr session, GitHub access or the scheduler are unavailable, report the specific blocker during startup.

## Startup integration

Package and enable the startup integration as part of configuring a project-manager profile. Use a Hermes `on_session_open` hook for the normal interactive launch. It checks the selected profile when the session opens and starts either manager setup or the interview. The setup is shared with manual recovery and skips internal manager and worker sessions so the manager's own Hermes launch cannot start another manager. See [Hermes session-open hooks](https://hermes-agent.nousresearch.com/docs/user-guide/features/hooks#on_session_open).

## Setup interview

When the project settings section is absent or required choices are missing, Hermes opens the interview in the same session and presents the first question. The derived `SKILL.md` contains the interview instructions.

Ask only for missing project choices, such as the target repositories, dashboard, worker backend, and GitHub App identity. Resolve repository and Herdr IDs through the project manager tools, and keep GitHub credentials in the configured App access. Save the answers in the selected profile. If the interview is interrupted, resume from the saved answers on the next run. Once the required settings are saved and validated, continue through the normal startup sequence in the same session.

## Project settings in the profile

Store the non-secret project settings directly in the selected profile's `config.yaml` under `skills.config.hermes-herdr-github-project-manager`. The setup session saves the settings there. For example, the profile could contain this partial configuration before setup resolves the repository and Herdr IDs.

```yaml
skills:
  config:
    hermes-herdr-github-project-manager:
      name: example-manager
      herdr:
        instance: default
      worker:
        agent: codex
        github_identity: milyin-hermes
      dashboard:
        repo: example-org/manager-dashboard
      repositories:
        example-org/project:
          kind: owned
          base: main
```

- `worker.agent` selects a backend supported by the Herdr agent manager. Use a backend name such as `codex` or `hermes`. The project manager passes this value as `--agent` when provisioning and starting the worker.
- `worker.github_identity` names the GitHub App assigned to the worker. Use `milyin-hermes` for this deployment. GitHub credentials come from the configured App access.

The profile also holds the manager name, Herdr instance and verified workspace ID, dashboard identity and repository, allowlisted target repositories, numeric IDs and base branches, limits, and skill update settings. See [Hermes skill configuration](https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills#config-settings-configyaml).

## How scripts find the settings

Hermes selects the profile when it starts. Manager scripts use that profile context to read the configured section directly, for example with Hermes's configuration reader and the key `skills.config.hermes-herdr-github-project-manager`. The manager session, setup commands and scheduled watchdog use the same profile and effective settings. The launcher supplies the profile context to background processes. The user selects the profile on the Hermes command line.

Keep generated task state, receipts and working files in the profile's private `github-project-manager/` directory. The profile's configuration is the single source of project settings. When those settings change, revalidate them before applying the workflow.
