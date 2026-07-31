## System Overview

This project is an open-source orchestration platform that coordinates multiple *existing* coding agents (Claude Code, Codex, Gemini CLI, OpenCode/Aider, and other adapters) to complete software-engineering tasks end-to-end — planning, implementation, review, and fixing — instead of implementing its own coding agent. It dynamically selects and switches between agents mid-task based on task type, repository context, required capabilities, past performance, availability, cost, and configurable routing policies, following structured workflows such as planner → implementer → reviewer → fixer. A canonical, agent-agnostic task context is carried across every handoff, and deterministic checks (tests, lint, types, security scans) — not an agent's self-report — decide whether work is actually done. As of 2026-07-31, no code has been written yet; this document describes the target architecture the requirements checklist and tool survey (see `Research.md`, `Decisions.md`) are converging on.

## High-Level Architecture

```
                          ┌─────────────────────────────┐
                          │   Canonical Task Context     │
                          │  (objective, requirements,   │
                          │   constraints, acceptance     │
                          │   criteria, decisions,        │
                          │   completed/remaining work,   │
                          │   failed attempts)            │
                          └───────────────┬──────────────┘
                                          │ read/write every handoff
                                          ▼
   task in ──▶ ┌───────────────┐   ┌───────────────┐   ┌───────────────┐   ┌───────────────┐
               │    Planner     │──▶│  Implementer   │──▶│    Reviewer    │──▶│     Fixer      │
               │ (Agent Router  │   │ (Agent Router  │   │ (Agent Router  │   │ (Agent Router  │
               │  picks agent)  │   │  picks agent)  │   │  picks agent,  │   │  picks agent)  │
               └───────┬───────┘   └───────┬───────┘   │  != implementer)│   └───────┬───────┘
                       │                   │            └───────┬───────┘           │
                       │                   ▼                    │                   │
                       │         ┌───────────────────┐          │                   │
                       │         │  Shared workspace   │◀────────┴───────────────────┘
                       │         │ (Git worktrees,      │
                       │         │  commits, diffs)      │
                       │         └─────────┬───────────┘
                       │                   ▼
                       │         ┌───────────────────┐        fail ──▶ back to Fixer
                       │         │ Deterministic check │──────┐
                       │         │ runner (tests, lint,│      │
                       │         │ types, security)    │      │
                       │         └─────────┬───────────┘      │
                       │                   │ pass              │
                       │                   ▼                   │
                       │         ┌───────────────────┐         │
                       └────────▶│ Human approval gate │◀──────┘
                                 │  (optional, policy-  │
                                 │  driven)             │
                                 └─────────┬───────────┘
                                           ▼
                                     task accepted

   Cross-cutting, always active:
   - Agent Router (agent selection/switching policy)
   - Session Registry (per-agent native resumable session IDs)
   - Handoff Builder (assembles context packet before every switch)
   - Event/Trace/Cost/Audit log (every step above emits normalized events)
```

## Module Breakdown

### Agent Router
Selects the most suitable agent for the current workflow stage given task type, repository context, required capabilities, prior performance, availability, cost, and configurable routing policy. Deterministic rules apply first (e.g., architecture/planning → Claude Code, implementation → Codex, large-context repo analysis → Gemini CLI/large-context agent, mechanical refactors → OpenCode/Aider); an LLM router is the fallback only for ambiguous cases. See ADR-002 for the routing/supervisor technology choice (LangGraph).

### Canonical Task Context Store
Holds the single source of truth for a task across all agent handoffs: objective, requirements, constraints, acceptance criteria, architectural decisions, completed work, remaining work, and unsuccessful attempts. Never mutated implicitly by an agent — only through the Handoff Builder. See Research topic 1 for why this exists (native session state cannot cross vendors).

### Session Registry
Maps each agent to its own native resumable session ID (where the underlying CLI supports one), so that when control returns to a previously used agent, the orchestrator resumes that native session rather than starting cold. Distinct from the Canonical Task Context Store — this is per-agent, vendor-specific bookkeeping, not shared task state.

### Handoff Builder
Assembles what an incoming agent actually receives before it starts: the canonical objective/constraints, relevant prior decisions, current Git diff, latest check failures, remaining work, the agent's own native session reference (if resuming), and relevant skill content (see ADR-001 — skills are injected explicitly, not left to auto-discovery).

### Shared Workspace (Git worktrees)
Each task/agent gets an isolated Git worktree (or container) so parallel and sequential agents can work without clobbering each other's edits; changes, commits, and diffs are the shared ground truth agents and the check runner both read from.

### Deterministic Check Runner
Runs a verification manifest (tests, lint, type checks, security scans, project-specific acceptance checks) and decides pass/fail from exit codes — never from an agent's self-reported "done." Failures route back to the Agent Router to pick a fixer, not necessarily the original implementer.

### Event/Trace/Cost/Audit Log
Every component above emits normalized events (agent switches, check results, review outcomes, human approvals/rejections) into a persistent, crash-recoverable log, enabling resumability, timeouts/retries/cancellation, and full audit history.

### Human Approval Gate
Optional, policy-configurable checkpoint before a change is accepted, independent of which agents were involved.

## Technology Stack

| Component | Technology (provisional) | Notes |
|---|---|---|
| External-agent execution/session runtime | Claw Orchestrator, *or* OpenHands Agent Canvas | Not yet finalized — see ADR-002 and ADR-003. Both provide multi-CLI sessions (Claude Code, Codex, Gemini CLI, others); neither does dynamic routing itself. |
| Routing / supervisor / workflow | LangGraph | Reused from prior project experience; nodes wrap CLI subprocess calls (`claude -p ...`, `codex exec ...`) exactly like LLM-calling nodes — see Research topic 4. |
| Canonical task context, checkpoints, audit trail | MongoDB | Chosen for crash-safe persistence of task state, decisions, failures, and session IDs — reused from prior stack. |
| Workspace isolation | Git worktrees (+ Docker/containers where needed) | Per-task/per-agent isolation for parallel and sequential execution. |
| Deterministic verification | Exit-code-based check runner (pytest, ruff/lint, mypy/type-check, bandit/security, project acceptance commands) | Explicitly not an LLM judge — see ADR-003 for why this ruled out relying on `/goal`-style LLM-judged completion. |
| Observability | Phoenix / OpenTelemetry | Reused from prior project experience for traces, events, errors, durations. |
| Deployment | Docker / WSL2 (Windows-compatible) | Local, no mandatory cloud dependency. |

## Changelog

### 2026-07-31 — Initial target architecture documented; no code written yet

Established the target architecture described above based on the project's 10-point requirements outline and the tool survey in `Research.md`. No implementation exists yet — this is a planning-stage document. Key decisions driving this shape: ADR-001 (explicit skill injection over CLI auto-discovery), ADR-002 (provisional Claw Orchestrator + LangGraph + MongoDB + Git worktrees + Phoenix stack), ADR-003 (OpenHands Agent Canvas kept open as an alternative execution layer, pending hands-on evaluation).

---
