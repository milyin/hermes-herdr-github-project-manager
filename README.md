# Hermes Herdr GitHub Project Manager

A Hermes skill and plugin for coordinating named workers from a dedicated Hermes terminal pane in Herdr. Workers have interactive Codex panes. Project-specific rules live in configurable prompts.

Install the skill, its Hermes plugin and the [companion pane and agent managers](doc/WORKFLOW.md#dependency-skill-requirements) in the same Hermes profile. Open the [profile setup](doc/PROFILE.md) and choose a [bundled workflow](workflows/README.md) or [create your own](doc/PROFILE.md#create-a-workflow). The Hermes [commands](doc/COMMANDS.md) expose the same worker and inspection operations the watchdog uses.

The [workflow specification](doc/WORKFLOW.md) defines the engine and prompt contracts. The [GitHub workflow](workflows/github/README.md) includes its own prompts, App launcher and installation guide.

## Tests

Run `python3 -B -m unittest discover -s tests -v` with PyYAML installed. Tests that exercise the bundled manager integrations use the Hermes shared gateway, Herdr and Codex against disposable profiles, panes and a local model fixture.
