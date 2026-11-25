# Project manager commands API

Use these commands in the Hermes interface for the selected project-manager profile. The manager can call the same operations through its tools. `PROMPT` is the non-empty text after the name or pane ID. `PANE_ID` is the exact ID returned by `run`.

| Command | Result and behavior |
| --- | --- |
| `/project-manager run NAME PROMPT` | Start a worker from the named configuration and submit its first prompt. Return its pane ID once submission is verified. |
| `/project-manager steer PANE_ID PROMPT` | Submit a follow-up in the same worker session after its previous result was delivered to the manager. Return the pane ID and submission status. |
| `/project-manager query PANE_ID` | Return the status of the pane's latest turn. For a finished turn, include its saved result message. Reading the result leaves its delivery status unchanged. |
| `/project-manager close PANE_ID` | Close a verified idle worker pane after its result was delivered or if it never received a turn. For an already absent pane, confirm the saved binding is gone. Preserve the saved result and report whether the pane is closed. |

**Results.** Successful `run` and `steer` return only after verified submission to the bound session. `query` reports `working`, `done` with the result message, or `unknown` with the reason. A confirmed interruption is a finished result with an interruption message.

**Refusals.** Report the cause, such as invalid arguments, unknown worker or pane, locked pane, busy or unread pane, working directory already in use, capacity, or quota. A refusal means the requested prompt was never submitted. Release unused reservations after a definitive refusal and close any newly created idle pane through the pane manager. If cleanup is blocked, report that pane ID. If submission may have taken effect, report `outcome_unknown` and the pane ID when known. Resolve the saved receipt before any retry.

**Scope.** Each pane ID is resolved within the selected profile's verified Herdr workspace and against its saved worker binding. `steer` binds the follow-up to the saved thread and last turn. Commands serialize admission with cron so simultaneous requests respect the same capacity. See [pane lifecycle and delivery](WORKFLOW.md#pane-lifecycle).
