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
| `/project-manager check` | Verify profile settings, the Herdr session and any bound workspace, GitHub identities and repository IDs, required wiki access, and host health. | Readiness and any failed checks. |
| `/project-manager show` | Read the latest recorded management pass in the selected profile. | Pass timestamp, dry-run setting, and result or failure. |
| `/project-manager versions` | Read the installed revisions and the canonical `main` revisions for the three skills. | Per-skill revision and update status, including any hold. |

## Dry run

| Command | Behavior | Result |
| --- | --- | --- |
| `/project-manager dry-run on` | Set `dry_run: true` in the selected profile. The manager reports what it would do on subsequent passes. | Dry run enabled. |
| `/project-manager dry-run off` | Verify profile readiness and set `dry_run: false`. The manager can resume project work as other checks permit. | Dry run disabled or the readiness blocker. |

Both commands use the same manager pane and watchdog. See [dry-run behavior](WORKFLOW.md#dry-run).

## Quota check

| Command | Behavior | Result |
| --- | --- | --- |
| `/project-manager quota-check on` | Save `quota_check: true` and use pane preparation to make the Codex quota receiver ready when needed. Use the fixed pacing rule for new Codex turns. | Quota check enabled and any quota hold. |
| `/project-manager quota-check off` | Save `quota_check: false`. New turns skip the project manager's quota check. Provider limits continue to apply. | Quota check disabled. |

See [the quota pacing rule](WORKFLOW.md#quota-check).

## Startup and recovery

| Command | Behavior | Result |
| --- | --- | --- |
| `/project-manager install-watchdog` | Enable the profile's scheduled watchdog or reuse the enabled job. Startup triggers its first run. | Job ID and enabled status. |
| `/project-manager prepare-panes` | Run the same [pane preparation](WORKFLOW.md#prepare-panes) as the scheduled watchdog. Verify the workspace and tracked panes and prepare the manager and any required quota pane. | Workspace ID, pane IDs and their status, and any blockers. |
| `/project-manager retire-uncertain ISSUE` | With dry run off, release a worker slot after the operator inspects the saved checkout and verified Herdr layout shows the saved pane is gone. Preserve the assignment and its evidence. | Issue and retirement status. |

## Work and maintenance

| Command | Behavior | Result |
| --- | --- | --- |
| `/project-manager follow-up ISSUE PROMPT` | Use the text after `ISSUE` as the follow-up prompt. Send it through the agent manager to the saved idle receiver, bound to the saved thread and prior turn. Require a verified live manager with dry run off. | Issue, exact new turn ID, and completed or pending status. |
| `/project-manager capability-gap SKILL SHORT-SLUG` | Create or reuse the routed issue for that capability gap in the selected skill's repository. Require a verified live manager with dry run off. | Repository and issue number. |
| `/project-manager sync SKILL --approved-head SHA` | With the supervisor stopped and dry run off, use the same verification as a scheduled update and fast-forward the selected skill to the approved canonical head. | Installed revision, update status, and any restart requirement. |
