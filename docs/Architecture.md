## System Overview

This project is an open-source orchestration platform that coordinates multiple *existing* coding agents (Claude Code, Codex, Gemini CLI, OpenCode/Aider, and other adapters) to complete software-engineering tasks end-to-end — planning, implementation, review, and fixing — instead of implementing its own coding agent. It dynamically selects and switches between agents mid-task based on task type, repository context, required capabilities, past performance, availability, cost, and configurable routing policies, following structured workflows such as planner → implementer → reviewer → fixer. A canonical, agent-agnostic task context is carried across every handoff, and deterministic checks (tests, lint, types, security scans) — not an agent's self-report — decide whether work is actually done. As of 2026-08-03, no code has been written yet; this document describes the target architecture the requirements checklist and tool survey (see `Research.md`, `Decisions.md`) are converging on.

A framing that shapes everything below: this is structurally a job scheduler whose workers are unreliable, expensive, and non-deterministic. The hard problems are crash recovery, idempotency, resource accounting, and state reconciliation — not prompting.

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
Selects the most suitable agent for the current workflow stage given task type, repository context, required capabilities, prior performance, availability, cost, and configurable routing policy. Deterministic rules apply first (e.g., architecture/planning → Claude Code, implementation → Codex, large-context repo analysis → Gemini CLI/large-context agent, mechanical refactors → OpenCode/Aider); an LLM router is the fallback only for ambiguous cases.

Availability and cost are read from the **Budget Ledger** rather than queried from vendors, because no target agent exposes remaining allowance (Research topic 6). Per ADR-006, the ledger holds a user-configured limit and `limit_type` per agent, accumulates consumption from per-call usage as an estimate for pre-emptive routing, and treats a rate-limited process exit as the authoritative exhaustion signal. Two rules bind the router: reserve headroom before dispatch, and price the cost of switching, since resuming an agent that still holds a native session is materially cheaper than cold-starting a different vendor.

### Canonical Task Context Store
Holds the single source of truth for a task across all agent handoffs: objective, requirements, constraints, acceptance criteria, architectural decisions, completed work, remaining work, and unsuccessful attempts. Never mutated implicitly by an agent — only through the Handoff Builder. See Research topic 1 for why this exists (native session state cannot cross vendors).

### Session Registry
Maps each agent to its own native resumable session ID (where the underlying CLI supports one), so that when control returns to a previously used agent, the orchestrator resumes that native session rather than starting cold. Distinct from the Canonical Task Context Store — this is per-agent, vendor-specific bookkeeping, not shared task state.

### Handoff Builder
Assembles what an incoming agent actually receives before it starts: the canonical objective/constraints, relevant prior decisions, current Git diff, latest check failures, remaining work, the agent's own native session reference (if resuming), and relevant skill content (see ADR-001 — skills are injected explicitly, not left to auto-discovery).

Per ADR-005, the packet is **reconstructed at entry** from artifacts written continuously during the previous run — worktree commits, plan step status, the streamed event log, and the last check result — and is never produced by the outgoing agent as an exit summary, which an agent killed by a rate limit would never live to write. Two further constraints: dead ends ("X was tried and fails because Y") are carried explicitly, or the incoming agent reattempts exactly what killed its predecessor; and the packet is itself budgeted and measured, since every handoff consumes the receiving agent's allowance — the very resource being managed.

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
| External-agent execution/session runtime | **Maestro** (maestro-flow) | Chosen 2026-08-03 — see ADR-004. `maestro delegate` provides background delegation to a different backend coding-agent CLI mid-task, which is the primitive the earlier 10-tool survey found missing everywhere. Supersedes the Claw Orchestrator / OpenHands Agent Canvas question left open by ADR-002 and ADR-003. Two constraints carry forward: its built-in decision gates are all post-execution, so a pre-execution plan-review checkpoint must be assembled manually; and its own completion signals stay subordinate to this project's deterministic checks. |
| Routing / supervisor / workflow | *Deferred* — LangGraph, Temporal, or hand-rolled | Maestro supplies a run/step lifecycle, so whether a separate workflow engine is needed underneath it is open — see Research topic 7. Langflow is ruled out as a runtime (it is a visual builder over LangChain, not a durable state machine); if a visual surface is wanted it belongs on top as an authoring/monitoring UI. Whatever is chosen, nodes wrap CLI subprocess calls (`claude -p ...`, `codex exec ...`) exactly like LLM-calling nodes — see Research topic 4. |
| Per-agent budget accounting | Ledger derived from recorded runs | Configured limits plus per-call usage as an estimate, with rate-limited process exits as the authoritative exhaustion signal — see ADR-006. No vendor exposes remaining allowance, and the four agents are not bounded by comparable quantities. |
| Canonical task context, checkpoints, audit trail | MongoDB | Chosen for crash-safe persistence of task state, decisions, failures, and session IDs — reused from prior stack. |
| Workspace isolation | Git worktrees (+ Docker/containers where needed) | Per-task/per-agent isolation for parallel and sequential execution. |
| Deterministic verification | Exit-code-based check runner (pytest, ruff/lint, mypy/type-check, bandit/security, project acceptance commands) | Explicitly not an LLM judge — see ADR-003 for why this ruled out relying on `/goal`-style LLM-judged completion. |
| Observability | Phoenix / OpenTelemetry | Reused from prior project experience for traces, events, errors, durations. |
| Deployment | Docker / WSL2 (Windows-compatible) | Local, no mandatory cloud dependency. |

## Changelog

### 2026-07-31 — Initial target architecture documented; no code written yet

Established the target architecture described above based on the project's 10-point requirements outline and the tool survey in `Research.md`. No implementation exists yet — this is a planning-stage document. Key decisions driving this shape: ADR-001 (explicit skill injection over CLI auto-discovery), ADR-002 (provisional Claw Orchestrator + LangGraph + MongoDB + Git worktrees + Phoenix stack), ADR-003 (OpenHands Agent Canvas kept open as an alternative execution layer, pending hands-on evaluation).

### 2026-08-03 — Execution layer settled on Maestro; handoff and budget mechanics specified

The open execution-layer question closed and two load-bearing mechanics were pinned down. Still no implementation.

* **Execution layer: Maestro** (ADR-004), replacing both candidates ADR-003 had left open. The deciding finding was that Claw Orchestrator and OpenHands differ only in which surrounding layer you inherit — coordination primitives versus platform infrastructure — while neither reduces this project's core custom work (Research topic 5). Maestro-flow and Flow-next then showed that mid-task delegation to a *different* agent CLI is already a solved primitive (Research topic 8), which is what the original survey had concluded was missing from every candidate.
* **Handoff Builder inverted** (ADR-005): packets are reconstructed at entry from continuously-written durable artifacts, never summarized at exit. This is the difference between the failover feature working and failing in exactly the case it exists for, and it is the first thing to build.
* **Agent Router constrained by measurement reality** (ADR-006): no target agent exposes remaining allowance, and the four are not bounded by comparable quantities — Gemini CLI's free tier is request-count limited, DeepSeek only exposes a balance. Routing is therefore estimation with a reactive override, not budget arithmetic (Research topic 6).
* **Stack table amended**: Claw Orchestrator withdrawn; Langflow ruled out as a runtime; LangGraph downgraded from chosen to deferred, since Maestro brings its own run/step lifecycle (Research topic 7). MongoDB, Git worktrees, the deterministic check runner, and Phoenix/OpenTelemetry are unchanged.

Also noted for implementation: the workspace lock (one active agent per worktree) and a per-step attempt cap that escalates to a different agent are not optional refinements — without them, concurrent worktree corruption and unbounded fix loops are the two failure modes that burn every agent budget at once.

---
