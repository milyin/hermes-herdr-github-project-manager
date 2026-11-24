# Project-manager workflow specification

Status: Draft. This document records proposed workflow decisions as they are discussed. The [operator contract](operator.md) describes the current implementation, which may differ from this specification.

## Quota and task dispatch

The project manager owns the quota policy and the decision to start an assignment. Before dispatch, it obtains current quota information through the agent manager's public interface and checks whether the account has enough quota under the project policy. If quota permits and the other dispatch checks pass, the project manager starts the worker through the agent manager. If quota does not permit a start or cannot be evaluated, the project manager leaves the assignment waiting.

The agent manager provides the quota information and the launch interface. The worker receives the task and works on it; it does not check quota or decide whether the assignment may start.

Implementation status: The current agent manager also enforces quota pacing when a new Codex turn is submitted. This draft changes the intended ownership, not the runtime. The current enforcement remains in place until the implementation changes.
