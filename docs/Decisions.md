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
| **Status (2026-08-03)** | **Partly superseded by ADR-004.** The Claw Orchestrator element is withdrawn — the execution/session layer is now Maestro. MongoDB, Git worktrees, the deterministic check runner, and Phoenix/OpenTelemetry stand unchanged. The LangGraph element is downgraded from "chosen" to "deferred": Maestro supplies its own run/step lifecycle, so whether a separate workflow engine is needed underneath it is an open question (see Research topic 7). |

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
| **Status (2026-08-03)** | **Resolved — neither.** Deeper comparison (Research topic 5) established that the two differ only in which surrounding layer is inherited, and that neither reduces the project's core custom work; a hands-on OpenHands trial was attempted and blocked before it could settle the question empirically (`Bugs.md` #1). A third option surfaced the same day and won: Maestro. Recorded as ADR-004 rather than here, because the outcome is a new choice rather than a resolution of the two-way comparison this ADR framed. |

---

## ADR-004 · Adopt Maestro as the external-agent execution and delegation layer

| Field | Detail |
|---|---|
| **Decision** | Use Maestro (maestro-flow) as the layer that starts, delegates to, monitors, and resumes external coding-agent CLIs, in place of both Claw Orchestrator (ADR-002) and OpenHands Agent Canvas (ADR-003). |
| **Date** | 2026-08-03 |
| **Context** | ADR-003 left the execution layer as an open two-way choice pending hands-on evaluation. That evaluation (Research topic 5) found the two candidates differ only in which surrounding layer you inherit — coordination primitives versus platform infrastructure — and that neither reduces the project's core custom work; the OpenHands trial that would have settled it was blocked by an endpoint connection failure (`Bugs.md` #1). Two Claude Code-native frameworks then surfaced, Maestro-flow (Ralph) and Flow-next, both of which already implement mid-task delegation to a *different* coding-agent CLI — the primitive the original 10-tool survey concluded was missing everywhere (Research topics 2, 8). |
| **Options Considered** | (A) Claw Orchestrator per ADR-002 — coordination primitives (Council, fan-out, worktree-backed parallelism) are closest to multi-agent coding work, but the advantage collapses if those features go unused beneath a custom routing layer. · (B) OpenHands Agent Canvas per ADR-003 — inherits sandboxes, UI, secrets, ACP, and deployment backends, but its abstraction is one conversation per manually selected agent profile, and its `/goal` completion signal is an LLM judge rather than the deterministic exit-code check this project requires. · (C) Flow-next — the most configurable out of the box: `plan-review` and `impl-review` as named commands, swappable review backend via `flowctl config set review.backend`, per-task reviewer routing, opt-in implementation delegation, and three autonomy levels over identical gates. · (D) Maestro-flow — `maestro delegate` as a background CLI-delegate primitive, `cli`-typed chain steps that hand a step to another backend agent, `decision` gates that insert fix loops, a canonical Session/Run lifecycle with persistent state, and a knowledge system for durable project context. |
| **Chosen Solution** | (D) Maestro. |
| **Rationale** | Judged the better fit for this project's shape: `maestro delegate` is a general-purpose delegation primitive rather than a fixed review pipeline, so it composes with a custom router instead of prescribing one; the Session/Run lifecycle and knowledge system supply persistence and cross-run context that would otherwise be built from scratch; and it is already installed and configured in this environment, which removes the setup cost that blocked the OpenHands trial. Flow-next is the more configurable of the two out of the box and remains the fallback if Maestro's post-execution-only decision gates prove too restrictive. |
| **Impact** | Replaces the "external-agent execution/session runtime" row in `Architecture.md`'s technology stack. Partly supersedes ADR-002: Claw Orchestrator is withdrawn and LangGraph is downgraded to deferred, since Maestro brings its own run/step lifecycle; MongoDB, Git worktrees, the deterministic check runner, and Phoenix/OpenTelemetry are unaffected. Resolves ADR-003 in the negative for both of its candidates. Two known constraints carry forward: Maestro's built-in decision gates are all post-execution, so a pre-execution plan-review checkpoint must be assembled manually with `maestro delegate`; and Maestro's own completion signals still have to be subordinated to this project's deterministic exit-code checks rather than trusted directly. |

---

## ADR-005 · Reconstruct the handoff packet on agent entry from durable artifacts; never summarize on agent exit

| Field | Detail |
|---|---|
| **Decision** | The context packet handed to an incoming agent is assembled fresh at entry from artifacts written continuously during the previous run. No design may depend on an outgoing agent producing a handoff summary as its final act. |
| **Date** | 2026-08-03 |
| **Context** | Recorded from the build-guidance work done this session. The project's headline behaviour is "agent A hits its limit, agent B resumes." The intuitive design — agent runs, finishes, writes a handoff summary, next agent reads it — fails at precisely the moment it exists for: an agent killed by a rate limit or a hard stop never reaches "finishes," never writes the summary, and leaves nothing behind. |
| **Options Considered** | (A) Outgoing agent writes a structured handoff summary before exiting — simplest and highest-fidelity when it works, but produces nothing in the exact failure case the feature targets. · (B) Orchestrator summarizes the transcript after the process exits — survives a hard kill, but requires a full transcript to have been persisted anyway, which is most of option (C). · (C) Make four artifacts durable-by-construction during the run and derive the packet from them at entry: worktree commits (agent instructed to commit per logical step, plus an orchestrator-side auto-commit timer, so a killed process still leaves a diff), plan step status persisted as `pending`/`in_progress`/`done`, the agent's event stream persisted as each event arrives rather than at process exit, and the last check result tied to a commit SHA. |
| **Chosen Solution** | (C). |
| **Rationale** | It makes resumption independent of *how* the previous run ended — clean completion, rate limit, timeout, crash, or a closed laptop all produce the same recoverable state. It also removes any dependency on a dying agent behaving correctly under duress. |
| **Impact** | Load-bearing for the "Handoff Builder" and "Canonical Task Context Store" in `Architecture.md`, and the first thing to build. Two consequences follow: the event stream must be written during the run, never buffered to the end, or the failure mode returns; and any run with a start timestamp but no end timestamp is an orphan from a crash, so restart must reconcile plan state against actual Git state rather than assuming an interrupted run did nothing. |

---

## ADR-006 · Model agent budgets as a configured ledger with reactive limit detection as the authoritative signal

| Field | Detail |
|---|---|
| **Decision** | Represent each agent's allowance as a normalized ledger entry with a user-configured limit and a `limit_type` (rolling window / daily requests / monetary / none), accumulate consumption from per-call reported usage as an *estimate*, and treat a non-zero exit carrying a rate-limit message as the *authoritative* exhaustion signal that sets a cooldown and triggers failover. |
| **Date** | 2026-08-03 |
| **Context** | Recorded from the build-guidance work done this session. The project statement routes work by "token budget of the coding agents," which presumes a readable remaining-tokens value. Research topic 6 established that no target agent exposes one, and that the four agents are not even bounded by the same kind of quantity — Gemini CLI's free tier is request-count limited rather than token limited, and DeepSeek is bounded only by account balance. |
| **Options Considered** | (A) Query remaining budget from each vendor before routing — not possible; no such API exists for any of the four. · (B) Track consumption client-side only and treat the running total as ground truth — drifts, because vendors count tokens in ways that cannot be fully replicated locally, and drift here causes mid-task deaths. · (C) Detect exhaustion purely reactively from process failure — accurate but purely after the fact, so every limit is discovered by losing work to it. · (D) Combine (B) and (C) with an explicit precedence: the estimate drives pre-emptive routing, the reactive signal overrides it and is what the system trusts. |
| **Chosen Solution** | (D). |
| **Rationale** | Preserves the ability to route *before* dispatching a job that will not fit, while ensuring that inevitable estimate drift degrades into a correct failover rather than a wrong routing decision. |
| **Impact** | Defines the "Agent Router" inputs in `Architecture.md`. Three practical consequences: vendor rate-limit exit codes and error strings belong in configuration rather than code, because vendors reword them; the router must reserve headroom before dispatch, since a mid-task death costs both the partial work and a cold-start handoff; and it must price switching in, since returning to an agent that still holds a native session is materially cheaper than cold-starting a different vendor. DeepSeek's place in the ledger remains unresolved — as a model rather than an agentic CLI, it is either wrapped in a harness accepting a custom OpenAI-compatible endpoint or restricted to non-agentic roles, which is a hands-on question. |

---
