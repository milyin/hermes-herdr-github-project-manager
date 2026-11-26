---
name: hermes-herdr-github-project-manager
description: Coordinate configured workers in Herdr from a Hermes project-manager session.
compatibility: Linux, the Hermes shared gateway and plugin API, Herdr, and the companion pane and agent manager skills.
metadata:
  version: "0.2.0"
  author: "milyin"
---

# Herdr project manager

Use `/project-manager` to inspect the configured workers, panes, quota and capacity. The same command starts and steers workers, reads and acknowledges reports, updates pane comments, and closes worker panes.

The bound manager conversation receives registered project-manager tools. Use those tools for worker and pane operations. The selected workflow prompt supplies task policy and decides how to act on results. The watchdog prepares panes and delivers reports through the same Hermes conversation.

See [profile setup](doc/PROFILE.md), the [commands](doc/COMMANDS.md), and the [workflow specification](doc/WORKFLOW.md).
