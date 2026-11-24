# Project manager commands API

These commands let the profile startup routine, the watchdog, or an operator run a single operation. The [workflow specification](WORKFLOW.md) defines when each operation is allowed. Normal first run invokes startup automatically when the user [opens the profile](PROFILE.md#first-run).

## Invocation and results

```text
python3 -B <manager-script> COMMAND [ARGUMENTS]
```

The script is `skills/hermes-herdr-github-project-manager/scripts/manager.py` in the selected profile. Run it in that profile's context. Hermes supplies the profile to its child processes, and each command reads the [project settings in that profile](PROFILE.md#project-settings-in-the-profile). `--help` lists commands and command arguments.

Successful operations write one JSON object to stdout with `ok: true` and a `result` field, then exit with code 0. Operational refusals and failures return `ok: false` with an `error` code and exit with code 1. Invalid command syntax exits with code 2. A successful command can report a non-ready condition in its result. For example, `check` may complete and return `ready: false`.

`ISSUE` identifies the source issue as `OWNER/REPO#N`. `SKILL` is one of `hermes-herdr-pane-manager`, `hermes-herdr-agent-manager`, or `hermes-herdr-github-project-manager`. `SHORT-SLUG` is a lowercase, hyphen-separated name for the capability gap. `SHA` is the exact Git commit ID.

## Diagnostics

| Command | Behavior | Result |
| --- | --- | --- |
| `check` | Verify profile settings, the Herdr workspace, GitHub identities and repository IDs, required wiki access, and host health. | Readiness and any failed checks. |
| `preview` | Run a private dry run using current routes, layout, quota and version information. Save the preview locally. | Proposed work and holds. |
| `show` | Read the latest recorded management pass in the selected profile. | Pass timestamp and result or failure. |
| `routes` | List the configured dashboard routing labels for target repositories. | Mapping from target repository to label. |
| `versions` | Read the installed revisions and the canonical `main` revisions for the three skills. | Per-skill revision and update status, including any hold. |

## Startup and recovery

| Command | Behavior | Result |
| --- | --- | --- |
| `quota-setup` | Provision the dedicated Codex quota receiver in the verified workspace or use the verified profile binding. | Pane ID and whether quota could be evaluated. |
| `activate` | Verify startup prerequisites and bind apply mode to the effective profile settings. | Activation status and any dispatch holds. |
| `install-watchdog` | Prepare the profile-local watchdog script and register or reuse the profile's enabled Hermes cron job with `--no-agent`. Startup triggers that job for its first run. | Job ID and enabled status. |
| `watchdog [--apply]` | Verify the manager in the bound workspace or start it through the pane manager. With `--apply`, require activation and check or start the apply manager. The default mode is preview. | Manager liveness or launch status, pane ID when known, and any startup hold. |
| `retire-uncertain ISSUE` | Release a worker slot after the operator inspects the saved checkout and verified Herdr layout shows the saved pane is gone. Preserve the assignment and its evidence. | Issue and retirement status. |

## Work and maintenance

| Command | Behavior | Result |
| --- | --- | --- |
| `follow-up ISSUE` | Read the new prompt from stdin. Send it through the agent manager to the saved idle receiver, bound to the saved thread and prior turn. Require a verified live apply manager. | Issue, exact new turn ID, and completed or pending status. |
| `capability-gap SKILL SHORT-SLUG` | Create or reuse the routed issue for that capability gap in the selected skill's repository. Require a verified live apply manager. | Repository and issue number. |
| `sync SKILL --approved-head SHA` | With the supervisor stopped, apply the same verification as a scheduled update and fast-forward the selected skill to the approved canonical head. | Installed revision, update status, and any restart requirement. |
