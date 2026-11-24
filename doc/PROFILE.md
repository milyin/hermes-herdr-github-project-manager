# Hermes project-manager profile setup

This guide describes the intended first run. The Hermes profile is the source of project settings and keeps the installed skills, saved task state, and watchdog job together.

## First run

Create a Hermes profile and open it once.

```sh
hermes profile create projectmanager
hermes -p projectmanager
```

In that session, ask Hermes to set up the profile as a GitHub project manager. Hermes uses any project settings already in the profile and asks for missing project choices, such as the repositories, dashboard and worker GitHub App account. If the profile is already configured, Hermes can proceed with those values. See [Hermes profiles](https://hermes-agent.nousresearch.com/docs/user-guide/profiles).

The setup session prepares the three skill checkouts, selects or creates the manager's workspace in the local Herdr session, verifies GitHub access, and runs the [startup sequence](WORKFLOW.md#profile-startup-and-periodic-passes). It starts the manager and registers its watchdog in the same profile once preflight and activation succeed. A later setup run reuses verified checkouts, workspace and watchdog. If a host prerequisite or GitHub permission is missing, report the specific blocker in that session.

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

- `worker.agent` selects a backend supported by the Herdr agent manager. The current agent manager supports `codex` and `hermes`. The project manager passes this value as `--agent` when provisioning and starting the worker.
- `worker.github_identity` names the GitHub App assigned to the worker. Use `milyin-hermes` for this deployment. GitHub credentials come from the configured App access.

The profile also holds the manager name, Herdr instance and verified workspace ID, dashboard identity and repository, allowlisted target repositories, numeric IDs and base branches, limits, and skill update settings. See [Hermes skill configuration](https://hermes-agent.nousresearch.com/docs/developer-guide/creating-skills#config-settings-configyaml).

## How scripts find the settings

Hermes selects the profile when it starts. Manager scripts use that profile context to read the configured section directly, for example with Hermes's configuration reader and the key `skills.config.hermes-herdr-github-project-manager`. The manager session, setup commands and scheduled watchdog use the same profile and effective settings. The launcher supplies the profile context to background processes. The user selects the profile on the Hermes command line.

Keep generated task state, receipts and working files in the profile's private `github-project-manager/` directory. The profile's configuration is the single source of project settings. When those settings change, revalidate them before applying the workflow.

## Current implementation

The current project manager reads a separate project YAML file and accepts `codex` as its worker backend. Reading the profile section, using the agent manager's supported backend values, and completing setup in one Hermes session require implementation. On existing installations, the setup should offer to import project settings into the profile before switching to the new reader. See the [operator guide for the current implementation](../references/operator.md).
