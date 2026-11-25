# Project manager commands API

Use `/project-manager` in the Hermes interface for the selected project-manager profile. It lists the available commands. The Hermes command handler, manager tools and cron use the same operation implementations, result fields and guards. The interface formats those results for the user. Cron consumes them directly.

## Worker commands

| Command | Result and behavior |
| --- | --- |
| `/project-manager run NAME PROMPT` | Render the named worker's prompt and append the task text. Return its pane ID after verified submission. Use the [named-parameter form](#named-parameters) for template values. |
| `/project-manager steer PANE_ID PROMPT` | Submit a follow-up in the same worker session after its previous result was delivered. Return the pane ID and submission status. |
| `/project-manager query PANE_ID [--turn TURN_ID]` | Return the worker turn's status and its full saved message when finished. Use the latest turn by default or request an exact saved turn. Reading leaves delivery status unchanged. |
| `/project-manager close PANE_ID` | Close a verified idle worker pane after its result was delivered or if it never received a turn. Confirm an already absent binding before reporting it closed. Preserve saved results. |

Query results identify the pane, turn ID, turn state and delivery state. A confirmed interruption is a finished turn with an interruption message. An unverifiable outcome is `unknown` with its reason.

Each pane ID is scoped to the selected profile's Herdr workspace and saved pane binding. `steer` binds the prompt to the saved thread and prior turn. `run` and `steer` use the same capacity and quota checks as cron. A definitive refusal reports the cause and means no prompt was submitted. Release unused reservations and close a newly created idle pane after a definitive refusal. If cleanup is blocked, report that pane ID. If a prompt may have been submitted, report `outcome_unknown` and the pane ID when known. Resolve the receipt before retrying.

## Named parameters

The Hermes command accepts repeated named values for worker prompt templates.

```text
/project-manager run NAME --param KEY=VALUE [--param KEY=VALUE ...] [-- PROMPT]
```

Pass the issue and task to the [default implementer prompt](IMPLEMENTER_PROMPT.md) like this.

```text
/project-manager run implementer --param REPO=owner/repo --param ISSUE=owner/repo#42 --param "TASK=Implement the issue and run tests"
```

Each `--param` takes one `KEY=VALUE` token. Quote the whole token with double quotes for spaces and use `\"` and `\\` inside quoted values for a literal quote and backslash. The handler reads text, with no shell expansion. With named parameters, text after `--` is an optional literal addition to the rendered prompt. Without named parameters, `run NAME PROMPT` passes the remaining text as the task. See [parameter resolution](PROFILE.md#prompt-files).

## Inspect the workspace

| Command | Information returned |
| --- | --- |
| `/project-manager workers` | Configured worker names, descriptions, agent, model and accepted template parameter names. Shows which parameters still need values for a run. |
| `/project-manager panes [FILTER ...]` | Workspace ID and matching managed pane records. Each record includes `pane_id`, `role`, `worker` when relevant, `suffix`, `presence`, `state`, latest local `turn_id`, `completed_at` when known, and `delivery`. |
| `/project-manager capacity` | Maximum working panes, occupied places, available places, and the pane IDs or unresolved launches counted against the limit. |
| `/project-manager quota [--worker NAME]` | For all configured Codex accounts or the account used by the named worker, show the associated workers, window usage, elapsed allowance, reset, observation time and reason for any hold. A worker without a Codex quota check reports that quota is inapplicable. |

`panes` supports `--role ROLE` (manager, worker or quota), `--worker NAME`, `--pane PANE_ID`, `--state STATE` (idle, starting, working, done or unknown), `--delivery DELIVERY` (none, unread, read or unknown) and `--present yes/no/unknown`. Combine filters to match all supplied criteria. For example, `/project-manager panes --role worker --state done --delivery unread` shows finished results awaiting the manager, oldest completion first with pane ID breaking ties. A tracked result can still appear when its pane is gone.

Inspection commands can refresh observations and preserve verified results. They leave delivery, prompt submission, and pane creation and closure to their corresponding operations.

## Run watchdog operations

| Command | Result and behavior |
| --- | --- |
| `/project-manager prepare-panes` | Run the same workspace and pane verification, creation and recovery used at the start of every cron tick. Return the workspace, verified panes and holds. |
| `/project-manager tick` | Run one complete [cron tick](WORKFLOW.md#cron-tick) in the same order and under the same lock as the scheduled run. Return the action taken or the reason it stopped, including any delivered worker pane and turn IDs. |
