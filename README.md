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

**Design phase — no implementation exists yet.** The tool search that ran through July is now closed. A 35-item requirements checklist was used to evaluate ten orchestration frameworks plus Command Code and OpenHands Agent Canvas, and none fully satisfied it; the recurring gap was that "multi-CLI execution runtime" and "dynamic cross-agent routing" were always separate concerns, with no candidate merging both.

That gap closed on 2026-08-03. **Maestro** was adopted as the external-agent execution and delegation layer (ADR-004) after hands-on evaluation ruled out Claw Orchestrator and OpenHands, and after Maestro-flow and Flow-next demonstrated that mid-task delegation to a *different* coding-agent CLI is already a solved primitive. Alongside it, the two mechanics the failover feature actually rests on were specified: handoff packets are reconstructed on agent entry from durable artifacts rather than summarized on exit (ADR-005), and agent budgets are modelled as a configured ledger with reactive limit detection as the authoritative signal, since no vendor exposes remaining allowance (ADR-006).

Current stack: Maestro (agent execution/delegation) · MongoDB (canonical task context, runs, events, audit) · Git worktrees (isolation) · exit-code-based check runner (verification) · Phoenix/OpenTelemetry (tracing). The workflow-engine slot — LangGraph, Temporal, or hand-rolled — is deliberately deferred, since Maestro supplies its own run/step lifecycle.

Next up is implementation: a single-agent end-to-end spine first, then the adapter interface and a second agent, then the budget ledger and failover. See `docs/Status.md` for the session-by-session log and `docs/Architecture.md` for the target architecture.

## Documentation

This project tracks its own history and reasoning in `docs/`, using the 5-file system described in `Markdown-Tracking-Guide.md`:

| File | Purpose |
|---|---|
| [`docs/Status.md`](docs/Status.md) | Chronological log of what happened, session by session |
| [`docs/Architecture.md`](docs/Architecture.md) | What the system looks like (target architecture, since no code exists yet) and how it got there |
| [`docs/Decisions.md`](docs/Decisions.md) | Numbered ADRs — why X was chosen over Y |
| [`docs/Research.md`](docs/Research.md) | What was learned from investigating tools, papers, and prior transcripts |
| [`docs/Bugs.md`](docs/Bugs.md) | Bug records — currently environment/tooling failures hit during evaluation; application defects once implementation begins |

Start with `docs/Status.md` for the latest session, or `docs/Architecture.md` for the big picture.
