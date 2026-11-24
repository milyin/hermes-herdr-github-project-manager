# Project manager commands API

Use `/project-manager` in the Hermes interface for the selected profile. The command applies to that profile's project manager. Startup runs automatically when you [open the profile](PROFILE.md#first-run).

## Use a command

Type `/project-manager` on its own to list the available commands. Add the command name and any arguments to run it.

```text
/project-manager check
/project-manager show
```

Hermes displays the result or explains why the command could not proceed. The [workflow specification](WORKFLOW.md) defines when each operation is allowed.

`ISSUE` identifies the issue as `OWNER/REPO#N`. `SKILL` is one of `hermes-herdr-pane-manager`, `hermes-herdr-agent-manager`, or `hermes-herdr-github-project-manager`. `SHORT-SLUG` is a lowercase, hyphen-separated name for the capability gap. `SHA` is the exact Git commit ID.

## Diagnostics

| Command | Behavior | Result |
| --- | --- | --- |
| `/project-manager check` | Verify profile settings, the Herdr workspace, GitHub identities and repository IDs, required wiki access, and host health. | Readiness and any failed checks. |
| `/project-manager preview` | Run a private dry run using labeled issues, layout, quota and version information. Save the preview locally. | Proposed work and holds. |
| `/project-manager show` | Read the latest recorded management pass in the selected profile. | Pass timestamp and result or failure. |
| `/project-manager versions` | Read the installed revisions and the canonical `main` revisions for the three skills. | Per-skill revision and update status, including any hold. |

## Startup and recovery

| Command | Behavior | Result |
| --- | --- | --- |
| `/project-manager quota-setup` | Provision the dedicated Codex quota receiver in the verified workspace or use the verified profile binding. | Pane ID and whether quota could be evaluated. |
| `/project-manager activate` | Verify startup prerequisites and bind apply mode to the effective profile settings. | Activation status and any dispatch holds. |
| `/project-manager install-watchdog` | Enable the profile's scheduled watchdog or reuse the enabled job. Startup triggers its first run. | Job ID and enabled status. |
| `/project-manager watchdog [--apply]` | Verify the manager in the bound workspace or start it through the pane manager. With `--apply`, require activation and check or start the apply manager. The default mode is preview. | Manager liveness or launch status, pane ID when known, and any startup hold. |
| `/project-manager retire-uncertain ISSUE` | Release a worker slot after the operator inspects the saved checkout and verified Herdr layout shows the saved pane is gone. Preserve the assignment and its evidence. | Issue and retirement status. |

## Work and maintenance

| Command | Behavior | Result |
| --- | --- | --- |
| `/project-manager follow-up ISSUE PROMPT` | Use the text after `ISSUE` as the follow-up prompt. Send it through the agent manager to the saved idle receiver, bound to the saved thread and prior turn. Require a verified live apply manager. | Issue, exact new turn ID, and completed or pending status. |
| `/project-manager capability-gap SKILL SHORT-SLUG` | Create or reuse the routed issue for that capability gap in the selected skill's repository. Require a verified live apply manager. | Repository and issue number. |
| `/project-manager sync SKILL --approved-head SHA` | With the supervisor stopped, apply the same verification as a scheduled update and fast-forward the selected skill to the approved canonical head. | Installed revision, update status, and any restart requirement. |
