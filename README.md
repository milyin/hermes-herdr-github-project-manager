# Hermes Herdr GitHub Project Manager

A portable Agent Skill for coordinating authorized GitHub work from one dedicated Hermes pane per profile. It uses the pane and agent manager skills as its only Herdr and worker interfaces. The project policy, publishing identity and dashboard destination live in local YAML rather than in the platform code.

The [workflow specification](doc/WORKFLOW.md) is the design draft for contributors. See [Hermes project-manager profile setup](doc/PROFILE.md) for the proposed first run and profile-based project settings.

## Install

Clone the whole repository as `hermes-herdr-github-project-manager` under the active profile's `skills` directory. Install the dependencies in the same profile and ensure the Python interpreter used to run the manager has PyYAML. Keep the configuration outside the checkout.

```sh
profile_name=default
profile_home=$(dirname "$(hermes -p "$profile_name" config path)")
export HERMES_HOME="$profile_home"
skill_dir="$profile_home/skills/hermes-herdr-github-project-manager"
git clone https://github.com/milyin/hermes-herdr-github-project-manager.git "$skill_dir"
install -d -m 700 "$profile_home/github-project-manager"
test -e "$profile_home/github-project-manager/config.yaml" ||
  install -m 600 "$skill_dir/examples/project.yaml" "$profile_home/github-project-manager/config.yaml"
python3 -B "$skill_dir/scripts/manager.py" --help
```

Edit the private configuration before running a pass. In particular, do not overwrite an existing configuration with the example. See the [operator contract](references/operator.md) for preflight, the exact start/cron sequence, authentication, routing, review handoffs, and recovery.

## Behavior

- A shared dashboard wiki gets separate pages per manager, with routed issues, PR checks, work queues, pane layout and health. Each owned repo has its own manager page. Work on external repositories is tracked centrally.
- Issue labels route work to one named instance. Local durable state, exact workspace IDs, and launch receipts prevent duplicate dispatch after a restart. Existing workers are not stopped when a new issue, quota hold, or restart is observed.
- Host pressure and worker capacity gate new work. For quota planning, the project manager uses the agent manager's `check-quota` result; the worker's receiver enforces admission at submission. Cleanup only removes eligible artifacts from the manager's own scratch directory.
- Capability gaps go through the configured repositories and ordinary reviewed PRs. The supervisor checks all three `main` branches every 30 minutes by default and fast-forwards eligible, unlocked installations at a quiet checkpoint. Preview only reports versions. The watchdog never installs updates.

The manager does not merge, change permissions, buy token capacity, adopt an arbitrary pane, or require its watchdog to run a model. PR publication and independent review are explicit handoffs; fork-only publication is not implemented. Linux is the supported controller platform.

## Tests

Run `python3 -B -m unittest discover -s tests -v` with PyYAML installed. Tests include real local Git wiki read-back and exercise safety gates without writing to live GitHub. Run `check` separately to verify actual repository, wiki, identity and pane prerequisites. Configuration success is not proof of an active manager or completed work.
