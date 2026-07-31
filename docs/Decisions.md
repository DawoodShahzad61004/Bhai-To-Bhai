## ADR-001 · Distribute shared skills to CLI agents via explicit prompt injection, not auto-discovery

| Field | Detail |
|---|---|
| **Decision** | Skills (e.g., a `graphify`-style `SKILL.md`) are stored once in a canonical `skills/<name>/SKILL.md` location and injected verbatim into the constructed prompt string for every headless agent call, rather than relying on each CLI's own auto-discovery mechanism. |
| **Date** | 2026-07-31 |
| **Context** | Claude Code and Codex discover skill/instruction files differently (`.claude/skills/` vs. `AGENTS.md`), and in headless/subprocess mode (`claude -p ...`, `codex exec ...`) there is no way to confirm whether auto-discovery actually fired. See Research topic 4. |
| **Options Considered** | (A) Rely on each CLI's native auto-discovery via a symlink (`.claude/skills/graphify` → canonical folder) and an `AGENTS.md` pointer for Codex — simplest, but unverifiable in one-shot subprocess calls. · (B) Duplicate skill content per-agent in each CLI's native format — avoids discovery ambiguity but creates drift between copies. · (C) Read the canonical `SKILL.md` at prompt-construction time in the orchestrator and inject its contents directly into every prompt sent to any agent, keeping the symlink/`AGENTS.md` setup only as a human-interactive fallback. |
| **Chosen Solution** | (C). The orchestrator's `load_skill(name)` reads `skills/<name>/SKILL.md` and prepends it to the task prompt before every `subprocess.run` call, for every agent adapter. |
| **Rationale** | Deterministic and agent-agnostic: the orchestrator controls and can log exactly what was sent, with no dependency on undocumented CLI discovery behavior. |
| **Impact** | Applies to every "Agent Router" node in `Architecture.md`. The symlink + `AGENTS.md` convention is retained only for humans running an agent interactively outside the orchestrator. |

---

## ADR-002 · Provisional stack: Claw Orchestrator + LangGraph + MongoDB + Git worktrees + Phoenix

| Field | Detail |
|---|---|
| **Decision** | Provisionally build the orchestration platform on Claw Orchestrator (external-agent runtime), LangGraph (routing/supervisor and workflow), MongoDB (canonical task context, checkpoints, decisions, audit events), Git worktrees (per-task isolation), and Phoenix/OpenTelemetry (tracing and observability), rather than adopting a single fully opinionated platform or combining several full orchestrators. |
| **Date** | 2026-07-31 |
| **Context** | The 35-item requirements checklist (see Research topic 2) is not fully satisfied by any single surveyed candidate. A stack needed to be chosen to make progress, reusing prior experience with LangGraph, MongoDB, Docker, and Phoenix/OpenTelemetry from other work. |
| **Options Considered** | (A) Adopt Maestro Orchestrate as a ready-made, opinionated platform — closest single-tool fit (9/10) but prescriptive about specialists/workflows. · (B) Combine multiple full orchestrators (Claw + Agent Orchestrator + The Pair + AgentOS) for maximum coverage — rejected: overlapping responsibilities increase integration time, failure points, and resource usage. · (C) Use Claw Orchestrator purely as the neutral external-agent execution/session runtime and build routing, canonical state, verification, and observability around it with tools already in use. |
| **Chosen Solution** | (C): Claw Orchestrator (agent sessions/runtime) + LangGraph (dynamic routing, agent switching, planner→implementer→reviewer→fixer workflow) + MongoDB (canonical task context, workflow checkpoints, decisions, failures, session IDs, audit events) + Git worktrees (parallel agent isolation) + a deterministic check runner (tests/lint/types/security via exit codes) + Phoenix/OpenTelemetry (traces, events, errors, duration) + Docker/WSL2 (reproducible Windows-compatible execution). |
| **Rationale** | Reuses an existing, working stack (LangGraph, MongoDB, Docker, Phoenix) instead of adopting an entirely new platform; only the agent-execution/runtime layer and a thin routing/canonical-state/verification layer are net-new work. Avoids the integration and reliability cost of running multiple overlapping orchestrators simultaneously. |
| **Impact** | Sets the default technology stack in `Architecture.md`. Superseded in part by ADR-003, which opens OpenHands Agent Canvas as an alternative to Claw Orchestrator specifically for the execution/session layer — LangGraph, MongoDB, worktrees, and Phoenix are unaffected by that reconsideration. |

---

## ADR-003 · Keep OpenHands Agent Canvas open as an alternative execution layer to Claw Orchestrator

| Field | Detail |
|---|---|
| **Decision** | Treat OpenHands Agent Canvas as a live alternative to Claw Orchestrator for the external-agent execution/session layer, without yet replacing ADR-002's choice — no final selection made. |
| **Date** | 2026-07-31 |
| **Context** | A follow-up comparison (Research topic 3) evaluated CommandCodeAI/command-code and OpenHands/openhands against the full checklist. Command Code was ruled out outright. OpenHands Agent Canvas covers Claude Code, Codex, Gemini CLI, and other ACP agents, ships official Docker/Windows support, and is MIT-licensed — but like Claw Orchestrator, it does not itself perform dynamic cross-agent task routing or use deterministic (non-LLM-judge) completion checks. |
| **Options Considered** | (A) Discard OpenHands, keep Claw Orchestrator per ADR-002 — simplest, no re-evaluation cost. · (B) Replace Claw Orchestrator with OpenHands Agent Canvas outright — premature given neither has been hands-on validated yet. · (C) Keep both open as candidates for the execution/session layer, to be decided after hands-on evaluation, while treating LangGraph-as-supervisor and MongoDB-as-canonical-state as settled regardless of which wins (per ADR-002). |
| **Chosen Solution** | (C). No production dependency has been added yet for either tool. |
| **Rationale** | The checklist-driven search is explicitly still open ("have not found a tool yet that fulfills all requirements... still searching") — locking in an execution-layer tool before hands-on testing would be premature. |
| **Impact** | `Architecture.md`'s "external-agent execution layer" is documented as pending-choice between Claw Orchestrator and OpenHands Agent Canvas. Revisit once either is prototyped against a real task; update this ADR's status rather than opening a new one when the choice is finalized. |

---
