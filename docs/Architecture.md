## System Overview

This project is an open-source orchestration platform that coordinates multiple *existing* coding agents (Claude Code, Codex, Gemini CLI, OpenCode/Aider, and other adapters) to complete software-engineering tasks end-to-end — planning, implementation, review, and fixing — instead of implementing its own coding agent. It dynamically selects and switches between agents mid-task based on task type, repository context, required capabilities, past performance, availability, cost, and configurable routing policies, following structured workflows such as planner → implementer → reviewer → fixer. A canonical, agent-agnostic task context is carried across every handoff, and deterministic checks (tests, lint, types, security scans) — not an agent's self-report — decide whether work is actually done. This document describes the **target** architecture the requirements checklist and tool survey (see `Research.md`, `Decisions.md`) converged on. As of 2026-08-04 the first code exists but none of the components below is implemented — see "Implementation Status" for what is actually built and how it maps onto this design.

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

## Implementation Status

The repository holds **two deliberately separate tracks**, which share no code and run on separate virtualenvs. Keeping them apart is the point of ADR-008; conflating them would let sandbox code drift into the orchestrator.

```
Bhai-To-Bhai/
├── orchestrator/            # The real system. Build Guide staged work.
│   └── preflight.py         # Stage 0 — the only orchestrator code that exists
├── yt-tutorial/             # Disposable LangGraph sandbox (ADR-008). Not part of the orchestrator.
│   ├── state.py             #   State + make_supervisor_node factory
│   ├── search_node.py       #   Two create_agent workers + the research supervisor
│   ├── research_builder.py  #   StateGraph assembly, compile, PNG render
│   ├── tools.py             #   scrape/outline/read/write/edit + tavily_tool
│   ├── llm_caller.py        #   Ported from RAG-work (ADR-009)
│   ├── logger_config.py     #   Ported from RAG-work
│   ├── llm_setup.py         #   Four ChatOpenAI clients on a custom endpoint
│   └── config.py            #   Retry / backoff / timeout / cooldown constants
├── docs/                    # The 5-file tracking system
├── Build-Guide.md           # Phased build order + the v0 acceptance test (A1–A8)
└── node_modules/            # maestro-flow 0.5.61, installed repo-locally (ADR-007)
```

**Orchestrator track.** `preflight.py` is the whole of it: it resolves each enabled agent binary from `cli-tools.json` (workspace entries overriding global), version-probes each one, pings MongoDB, and confirms `git worktree` support, exiting non-zero with a per-item report. It corresponds to Build Guide Stage 0 and to the Prevention note on `Bugs.md` #2. It currently fails two of its own checks (`Bugs.md` #3). None of the modules described above this section — Agent Router, Canonical Task Context Store, Session Registry, Handoff Builder, Shared Workspace, Check Runner, Event Log, Approval Gate — has any implementation.

**Sandbox track.** A hierarchical supervisor graph that compiles and renders but has never been invoked; two defects are waiting on its first run (`Bugs.md` #5, #6). Its architectural value is the mapping it makes concrete, and the one place that mapping breaks:

| Sandbox construct | Corresponds to | Where the correspondence fails |
|---|---|---|
| `make_supervisor_node` → routing decision | Agent Router | The sandbox supervisor routes on model output alone; the real router routes on ledger state, headroom, and switching cost (ADR-006) |
| `Command(goto=..., update=...)` | Handoff Builder packet | An in-process handoff is *given* by a cooperating node; a real packet must be *reconstructed* after a hard kill (ADR-005) |
| `State(MessagesState)` | Canonical Task Context Store | Graph state lives in process memory; canonical context must survive process death |
| `create_agent` worker node | Agent adapter | Workers are LLM agents inside one process; agents here are external vendor CLI subprocesses |
| `FINISH` → `END` | Deterministic check pass | The supervisor decides completion; here exit codes do, never an agent's claim |
| `llm_caller.py` FIFO gate + 429 handling | Budget ledger reactive signal · workspace lock | Detects HTTP 429s with structured headers; the ledger must detect process exits with configurable message strings (`Research.md` topic 11) |

The single decisive difference, and the reason the orchestrator cannot be a thin wrapper over a graph framework: in the sandbox the workers are constructed inside one Python process and hand control to each other cooperatively, whereas here they are separate vendor processes that can be killed mid-sentence by a rate limit. Everything ADR-005 exists to solve is what an in-process `Command` handoff never has to.

## Technology Stack

| Component | Technology (provisional) | Notes |
|---|---|---|
| External-agent execution/session runtime | **Maestro** (maestro-flow) v0.5.61, **installed repo-locally** | Chosen 2026-08-03 — see ADR-004. `maestro delegate` provides background delegation to a different backend coding-agent CLI mid-task, which is the primitive the earlier 10-tool survey found missing everywhere. Supersedes the Claw Orchestrator / OpenHands Agent Canvas question left open by ADR-002 and ADR-003. Two constraints carry forward: its built-in decision gates are all post-execution, so a pre-execution plan-review checkpoint must be assembled manually; and its own completion signals stay subordinate to this project's deterministic checks. Since 2026-08-04 it is a repo-local npm dependency at `node_modules/.bin/maestro` and is **not on `PATH`** (ADR-007) — every invocation, from orchestrator code included, must resolve it explicitly. |
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

### 2026-08-04 — First code; build order recorded; Maestro rescoped to a repo-local dependency; a working multi-agent graph exists in a sandbox that is deliberately not this system

The target architecture above is unchanged. What changed is that it now has a build order, a first artifact, a deployment constraint on one stack row, and — from a parallel learning track — a sharper account of what this project cannot borrow from graph frameworks.

* **Build order and acceptance criteria recorded** in `Build-Guide.md`: Stages 0–8, and the v0 target decomposed into eight testable assertions. **A4** — a handoff packet built with the outgoing agent having written nothing on exit — is the assertion that actually tests the design; A1–A3 and A5–A8 can all pass in a system that still dies on a real rate limit. This is the operational form of ADR-005 and should be read as its acceptance test.
* **Stage 0 exists**: `orchestrator/preflight.py`. This is the first code in the repository and the first component of "Implementation Status" above.
* **Maestro is now a repo-local npm dependency** and is no longer on `PATH` (ADR-007). This is a deployment fact with an architectural consequence: **executable resolution is a shared concern, not a per-call-site detail.** The preflight broke on its own Maestro probe within hours of the migration (`Bugs.md` #3) because that one lookup was hardcoded while agent lookups were configurable. Before the Stage 4 adapter interface exists, resolution should be a single path with explicit precedence — config entry → project-local `node_modules/.bin` → `PATH` — or the same bug reappears once per vendor.
* **v0 scope narrowed to Codex + Claude Code.** `gemini`, `opencode`, and `aider` are not installed on this machine, and `~/.maestro/cli-tools.json` enables only `codex`. The multi-adapter design is unaffected; the *demonstration* of it now needs two agents rather than four, which is sufficient for the failover assertion.
* **Implementation Status section added** above, including the construct-by-construct mapping between the LangGraph sandbox (ADR-008) and this design. Every mapping holds at the level of shape; all of them break at the level of process boundaries. The supervisor pattern, `Command(goto=..., update=...)` handoffs, and shared graph state all presume workers that live inside one process and exit cooperatively. Here they are vendor CLIs that can be `SIGKILL`ed mid-sentence, which is the entire premise of ADR-005 and has no counterpart in the graph framework's handoff primitive.
* **The supervisor's single-point-of-failure property carries over, and is worse here.** In the tutorial a dead supervisor stops the run. In this system the Agent Router holds the budget ledger and the workspace locks, so its own crash strands every in-flight worktree — which is the same argument ADR-005 makes for orphan reconciliation on restart, arriving from a different direction.
* **ADR-006's reactive-detection half has a working reference implementation**, one layer down: `yt-tutorial/llm_caller.py` (ported per ADR-009) implements a FIFO gate, a shared token-reset deadline so a limit is waited out once rather than once per consumer, an adaptive cooldown, and an abort threshold. Three of those transfer to the budget ledger conceptually; the cooldown *formula* does not, because it presumes a readable remaining/limit pair that no coding-agent vendor exposes (`Research.md` topics 6, 11). The FIFO gate is the closer reuse candidate — for the workspace lock rather than the ledger.
* **A silent-degradation failure mode observed in practice**, worth carrying into the ledger design: that module's cooldown floor quietly becomes `0.0` against a non-Groq endpoint and logs only at debug level (`Bugs.md` #6). A derived safety value defaulting to "no protection" without a visible signal is exactly the shape of bug the budget ledger is most exposed to. An unreadable consumption figure must surface as a **degraded** ledger state, never as an implicit zero.

---
