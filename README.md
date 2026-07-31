# Bhai-To-Bhai

An open-source, locally deployable orchestration platform that coordinates multiple *existing* coding agents (Claude Code, Codex, Gemini CLI, OpenCode/Aider, and other adapters) to complete software-engineering tasks end-to-end — from initial planning through final verification — instead of implementing its own coding agent.

## What it does

- Dynamically selects the most suitable agent for each stage of a task based on task type, repository context, required capabilities, prior performance, availability, cost, and configurable routing policies.
- Switches between agents *during* a single task, following structured workflows such as planner → implementer → reviewer → fixer.
- Maintains one canonical task context (objective, requirements, constraints, acceptance criteria, decisions, completed/remaining work, failed attempts) across every agent handoff.
- Tracks each agent's native resumable session reference so a previously used agent can pick up where it left off when control returns to it.
- Runs deterministic verification (tests, linting, type checks, security scans, project-specific acceptance checks) instead of trusting an agent's own claim of completion.
- Routes failed checks and review findings to the most appropriate agent for correction, then re-verifies and independently reviews before accepting changes.
- Persists workflow state, logs, traces, costs, and audit history so long-running tasks are observable, resumable, and recoverable after interruption — including optional human approval gates.
- Deploys locally or in containers, with an adapter model for adding new coding-agent CLIs, and never claims to transfer hidden model state between vendors — only explicit context and session references.

## Status

This project is in the requirements and tool-survey phase — no implementation exists yet. A 35-item requirements checklist was used to evaluate ten existing orchestration frameworks plus two additional candidates (Command Code, OpenHands Agent Canvas); none fully satisfies it, so the search continues. See `docs/Status.md` for the current state and `docs/Architecture.md` for the target architecture this is converging on.

## Documentation

This project tracks its own history and reasoning in `docs/`, using the 5-file system described in `Markdown-Tracking-Guide.md`:

| File | Purpose |
|---|---|
| [`docs/Status.md`](docs/Status.md) | Chronological log of what happened, session by session |
| [`docs/Architecture.md`](docs/Architecture.md) | What the system looks like (target architecture, since no code exists yet) and how it got there |
| [`docs/Decisions.md`](docs/Decisions.md) | Numbered ADRs — why X was chosen over Y |
| [`docs/Research.md`](docs/Research.md) | What was learned from investigating tools, papers, and prior transcripts |
| [`docs/Bugs.md`](docs/Bugs.md) | Bug records once implementation begins (currently empty) |

Start with `docs/Status.md` for the latest session, or `docs/Architecture.md` for the big picture.
