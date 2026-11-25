# Hermes Herdr GitHub Project Manager

A Hermes skill design for coordinating workers in Herdr from one dedicated Hermes TUI manager pane per profile. Codex workers have interactive panes for operator input. The [engine](doc/WORKFLOW.md#workflow-and-engine) manages panes and turns. The workflow's prompts supply project-specific behavior.

The [workflow specification](doc/WORKFLOW.md) defines the target behavior. The [profile setup](doc/PROFILE.md) covers configuration and first run, and the [commands API](doc/COMMANDS.md) covers the Hermes interface. See the [manager](doc/MANAGER_PROMPT.md), [implementer](doc/IMPLEMENTER_PROMPT.md), and [reviewer](doc/REVIEWER_PROMPT.md) prompt examples.

## Tests

Run `python3 -B -m unittest discover -s tests -v` with PyYAML installed.
