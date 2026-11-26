# Hello world workflow

This local workflow delegates a greeting to one worker and delivers the result to the manager.

## Profile settings

Follow the [main installation](../../doc/PROFILE.md#install-and-open-the-profile), then use these settings under `skills.config.hermes-herdr-github-project-manager`. Supply your models and existing working directories.

```yaml
enabled: true
herdr:
  instance: default
max_working_panes: 1
manager:
  name: projectmanager
  model: YOUR_HERMES_MODEL
  working_directory: /absolute/path/to/manager
  description: Demonstrate local worker delegation
  prompt_file: skills/hermes-herdr-github-project-manager/workflows/hello-world/MANAGER_PROMPT.md
workers:
  - name: hello
    pane_tag: H
    agent: codex
    model: YOUR_CODEX_MODEL
    working_directory: /absolute/path/to/worker
    description: Send a demo greeting
    prompt_file: skills/hermes-herdr-github-project-manager/workflows/hello-world/WORKER_PROMPT.md
```

Run `/project-manager` and verify that the first scheduled run leaves a `hello` worker pane and delivers its greeting.
