# Hermes Herdr GitHub Project Manager

A Hermes skill and plugin for coordinating named workers from a dedicated Hermes terminal pane in Herdr. Workers have interactive Codex panes. Project-specific rules live in configurable prompts.

Install the skill, its Hermes plugin and the [companion pane and agent managers](doc/WORKFLOW.md#dependency-skill-requirements) in the same Hermes profile. Set up the [profile](doc/PROFILE.md) and open it. The Hermes [commands](doc/COMMANDS.md) expose the same worker and inspection operations the watchdog uses.

The [workflow specification](doc/WORKFLOW.md) defines the engine and prompt contracts. The GitHub example includes a [manager](doc/MANAGER_PROMPT.md), [implementer](doc/IMPLEMENTER_PROMPT.md), and [reviewer](doc/REVIEWER_PROMPT.md).

## Tests

Run `python3 -B -m unittest discover -s tests -v` with PyYAML installed. Tests that exercise the bundled manager integrations use the Hermes shared gateway, Herdr and Codex against disposable profiles, panes and a local model fixture.
