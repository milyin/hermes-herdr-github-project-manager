# Project manager commands API

Use `/project-manager` in the Hermes interface for the selected project-manager profile, including the manager pane's TUI. It lists the available commands. The Hermes command handler, manager tools and cron share the [engine's](WORKFLOW.md#workflow-and-engine) operation implementations, result fields and guards. The interface formats those results for the user. Cron consumes them directly.

## Manager tools

Hermes gives the bound manager gateway session these registered tools and their argument schemas. Each calls the corresponding operation below. The engine supplies the profile and workspace from the session binding.

| Tool | Arguments |
| --- | --- |
| `project_manager_run` | `name` and optional `working_directory`, `parameters`, and `prompt`, under the same rules as `run`. |
| `project_manager_steer` | `pane_id` and `prompt`. |
| `project_manager_query` | `pane_id` and optional `turn_id`. |
| `project_manager_mark_read` | `pane_id`, `turn_id`, and `attention_id` for an escalation. |
| `project_manager_set_comment` | `pane_id` and `comment`. |
| `project_manager_close` | `pane_id`. |
| `project_manager_workers` | None. |
| `project_manager_panes` | Optional `role`, `worker`, `pane_id`, `state`, `delivery`, `presence`, and `lock` filters. |
| `project_manager_capacity` | None. |
| `project_manager_quota` | Optional `worker`. |

Pane preparation and `tick` are available through the operator commands and cron. The manager uses the tools above within its turn.

## Worker commands

| Command | Result and behavior |
| --- | --- |
| `/project-manager run NAME PROMPT` | Render the named worker's prompt and append the task text. Return its pane ID after verified submission. Use the [option form](#named-parameters) for template values or a separate working directory. |
| `/project-manager steer PANE_ID PROMPT` | Submit a follow-up in the same idle worker session if a working slot is available. The previous result may be `READ` or `UNREAD`. Return the pane ID and submission status. |
| `/project-manager query PANE_ID [--turn TURN_ID]` | For an open worker pane, return the latest or requested native turn and its full message when finished. If the launch or admission is unresolved, return the verified pane and launch details even before there is a thread or turn ID. Reading leaves delivery status unchanged. |
| `/project-manager mark-read PANE_ID TURN_ID [--attention ATTENTION_ID]` | Acknowledge handling the exact finished result or worker escalation on an open pane. Supply the attention ID for an escalation. Return `read` when already acknowledged or when the status is updated. Refuse stale IDs or a locked pane. |
| `/project-manager set-comment PANE_ID COMMENT` | Set the managed worker pane's single-line comment and return it after verification. Pass `""` to clear it. The pane manager preserves the displayed ID and status. Refuse a locked pane. |
| `/project-manager close PANE_ID` | Close a worker pane with a verified idle, finished or unused session through Herdr's guarded operation. The pane manager can mark the displayed ID `DEAD` and return `close_deferred` if zoom blocks the close or the native guard is unavailable. Pane preparation retries eligible closes. Verify absence in the bound workspace before reporting a pane already closed. |

Query results identify the pane, native turn ID, turn state, delivery state and the named parameters from the worker's run. A confirmed interruption is a finished turn with an interruption message. An unverifiable outcome is `unknown` with its reason.

Each pane ID is scoped to the selected profile's Herdr workspace and verified pane binding. `steer` targets the bound thread and requires it to be idle at admission. `run` and `steer` use the same capacity and quota checks as cron. A provisioning failure includes `phase: provision`, `admission: not_attempted` and the pane ID when available. After submission begins, an ambiguous receipt returns `outcome_unknown` with `admission: unknown` and the pane ID. Use `query` or `capacity` to reconcile the launch before retrying. An unresolved open launch occupies a slot. A verified turn follows normal capacity rules.

For direct interaction with a Codex worker, zoom its pane and use the Codex prompt in that pane. The engine resumes automation after unlock and verification of the same thread. A `DEAD` mark on the displayed pane ID indicates a [deferred close](WORKFLOW.md#deferred-close). See [interactive Codex workers](WORKFLOW.md#interactive-codex-workers).

## Named parameters

The Hermes command accepts repeated named values declared in the worker's settings. Supply a value for each parameter declared as `null` and override scalar defaults as needed. A command cannot declare a new parameter.

```text
/project-manager run NAME [--cwd ABSOLUTE_DIR] [--param KEY=VALUE ...] [-- PROMPT]
```

The implementer in the [profile example](PROFILE.md#settings) accepts an issue or an existing PR. The other reference defaults to `none`.

```text
/project-manager run implementer --param REPO=owner/repository --param ISSUE=owner/repository#42 --param "TASK=Implement the issue and run tests"
/project-manager run implementer --param REPO=owner/repository --param PR=https://github.com/owner/repository/pull/27 --param "TASK=Address the review feedback"
```

Each `--param` takes one `KEY=VALUE` token. Quote the whole token with double quotes for spaces and use `\"` and `\\` inside quoted values for a literal quote and backslash. The handler reads text, with no shell expansion. `--cwd` selects an existing absolute working directory for this run. With options, text after `--` is an optional literal addition to the rendered prompt. In the short form, `run NAME PROMPT` passes the remaining text as the task. See [parameter resolution](PROFILE.md#prompt-files).

## Inspect the workspace

| Command | Information returned |
| --- | --- |
| `/project-manager workers` | Configured worker names, pane tags, descriptions, agent, model and declared template parameters. Shows which parameters require a value for a run. |
| `/project-manager panes [FILTER ...]` | Workspace ID and matching managed panes from the live Herdr layout. Each record includes the native `pane_id`, `role`, `worker` when relevant, displayed `status` and `comment`, `presence`, `lock`, effective `state`, latest native `turn_id` when known, `completed_at` when known, `delivery`, and any pending closure. A current escalation includes `attention_id`, `state`, `reason`, and `observed_at`. |
| `/project-manager capacity` | Maximum working panes, occupied pane IDs, available slots, `unresolved_launches` with admission and presence, and which workers pass the [scripted launch preflight](WORKFLOW.md#cron-tick). Report the hold reason for each unavailable worker. |
| `/project-manager quota [--worker NAME]` | For all configured Codex accounts or the account used by the named worker, show the associated workers, window usage, elapsed allowance, reset, observation time and reason for any hold. A worker without a Codex quota check reports that quota is inapplicable. |

`panes` supports `--role ROLE` (manager, worker or quota), `--worker NAME`, `--pane PANE_ID`, `--state STATE` (idle, starting, working, blocked, suspended, done or unknown), `--delivery DELIVERY` (none, unread, read or unknown), `--present yes/no/unknown`, and `--lock locked/unlocked/unknown`. Combine filters to match all supplied criteria. `/project-manager panes --role worker --delivery unread` shows pending results and escalations for open panes, oldest first with pane ID breaking ties. Add `--state done` to show only finished results.

Zoom-locked panes have effective state `working`. Unlocked panes distinguish an agent waiting for input (`blocked`) from an operating-system suspension (`suspended`). For the manager pane, `panes` uses gateway turn state and reports whether the TUI is attached to the bound conversation. Use `query` to inspect a native turn result even while its pane is locked. Inspection commands can refresh observations. They leave delivery, prompt submission, and pane creation and closure to their corresponding operations.

## Run watchdog operations

| Command | Result and behavior |
| --- | --- |
| `/project-manager prepare-panes` | Run the workspace and pane verification, creation and recovery operations shared with cron. Return the workspace, verified panes and holds. |
| `/project-manager tick` | Run one complete [cron tick](WORKFLOW.md#cron-tick) in the same order and under the same lock as the scheduled run. Return the action taken or the reason it stopped, including the pane and turn IDs and any attention ID for a submitted report. |
