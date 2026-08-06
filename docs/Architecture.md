## System Overview

This project is an open-source orchestration platform that coordinates multiple *existing* coding agents (Claude Code, Codex, Gemini CLI, OpenCode/Aider, and other adapters) to complete software-engineering tasks end-to-end — planning, implementation, review, and fixing — instead of implementing its own coding agent. It dynamically selects and switches between agents mid-task based on task type, repository context, required capabilities, past performance, availability, cost, and configurable routing policies, following structured workflows such as planner → implementer → reviewer → fixer. A canonical, agent-agnostic task context is carried across every handoff, and deterministic checks (tests, lint, types, security scans) — not an agent's self-report — decide whether work is actually done. This document describes the **target** architecture the requirements checklist and tool survey (see `Research.md`, `Decisions.md`) converged on. As of 2026-08-05 the first code exists but none of the components below is implemented — see "Implementation Status" for what is actually built and how it maps onto this design.

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
├── yt_tutorial/             # Disposable LangGraph sandbox (ADR-008). Not part of the orchestrator.
│   ├── main.py              #   Entry point: logging setup, request id, stream the run
│   ├── config.py            #   Every tunable constant, each with why it holds that value
│   ├── prompts.py           #   Every prompt string (ADR-012)
│   ├── logging_config.py    #   One run_logs/<app>_<timestamp>.debug.log per run (ADR-013)
│   ├── llm/
│   │   ├── clients.py       #     Provider-switchable client construction
│   │   ├── invoker.py       #     Ported from RAG-work (ADR-009) + run_with_deadline (ADR-016)
│   │   └── tgi_compat.py    #     OpenAI wire-format shim on the httpx transport (ADR-010)
│   ├── tools/               #   documents.py · web.py · code.py
│   ├── teams/
│   │   ├── state.py         #     State schema (MessagesState + next)
│   │   ├── supervisor.py    #     Routing, termination guards (ADR-011), run_worker (ADR-016)
│   │   ├── research.py      #     search / web_scraper workers + compiled team graph
│   │   ├── writing.py       #     doc_writer / note_taker / chart_generator + team graph
│   │   ├── orchestrator.py  #     Top-level graph over both teams
│   │   └── visualization.py #     Shared PNG rendering
│   ├── claude_agents/       #   Second implementation of every node, on the Claude Code CLI (ADR-014)
│   │   ├── cli.py           #     The only place the CLI is invoked; ClaudeResult mirrors LLMResult
│   │   ├── router.py        #     Drop-in for make_supervisor_node; keeps the repeat guard
│   │   ├── agents.py        #     One ClaudeAgent per worker; briefs imported verbatim from prompts.py
│   │   ├── mcp_bridge.py    #     Serves this project's tools to the CLI over MCP (ADR-015)
│   │   └── prompts.py       #     Pipeline framing only — no role rewritten
│   └── temp_agents/         #   Standalone two-agent demo: Claude Code + Codex, run concurrently
│       ├── supervisor.py    #     Submits both before collecting either; then verifies the files
│       ├── runners.py       #     run_claude / run_codex — the two subprocess adapters
│       └── agents.py        #     Backend per agent, chosen in config.py
├── docs/                    # The 5-file tracking system
├── Build-Guide.md           # Phased build order + the v0 acceptance test (A1–A8)
└── node_modules/            # maestro-flow 0.5.61, installed repo-locally (ADR-007)
```

**Orchestrator track.** `preflight.py` is the whole of it: it resolves each enabled agent binary from `cli-tools.json` (workspace entries overriding global), version-probes each one, pings MongoDB, and confirms `git worktree` support, exiting non-zero with a per-item report. It corresponds to Build Guide Stage 0 and to the Prevention note on `Bugs.md` #2. It currently fails two of its own checks (`Bugs.md` #3). None of the modules described above this section — Agent Router, Canonical Task Context Store, Session Registry, Handoff Builder, Shared Workspace, Check Runner, Event Log, Approval Gate — has any implementation.

**Sandbox track.** As of 2026-08-06 the sandbox runs the same three-level hierarchy on **two interchangeable agent implementations**, selected by configuration (ADR-014): the original LangChain agents against a local TGI endpoint, or the Claude Code CLI driven as a subprocess. The graphs are identical either way — only who does the thinking differs — which is what turned `Bugs.md` #15 from an open question into a measured one: the same graph, the same prompts, and the same request routed research → writing → FINISH correctly and produced a grounded document on the CLI path, so the topology is sound and the 8B router is the constraint. A separate `temp_agents/` demo runs Claude Code and Codex concurrently against two files and then verifies them. Getting all of this to work produced nineteen defects across two days (`Bugs.md` #7–#25) and seven decisions that apply to the orchestrator (ADR-010 through ADR-016). Its architectural value is the mapping it makes concrete, and the places that mapping breaks:

| Sandbox construct | Corresponds to | Where the correspondence fails |
|---|---|---|
| `make_supervisor_node` → routing decision | Agent Router | The sandbox supervisor routes on model output alone; the real router routes on ledger state, headroom, and switching cost (ADR-006) |
| `Command(goto=..., update=...)` | Handoff Builder packet | An in-process handoff is *given* by a cooperating node; a real packet must be *reconstructed* after a hard kill (ADR-005) |
| `State(MessagesState, next)` | Canonical Task Context Store | Graph state lives in process memory; canonical context must survive process death. Note LangGraph *silently drops* updates to undeclared keys (`Bugs.md` #5a) — a canonical store cannot inherit that |
| `create_agent` worker node | Agent adapter | Workers are LLM agents inside one process; agents here are external vendor CLI subprocesses |
| `llm/tgi_compat.py` transport shim | Agent adapter's vendor normalization | Absorbs one server's wire-format quirks below every consumer; each vendor CLI needs the same treatment inside its own adapter (ADR-010) |
| `FINISH` → `END` | Deterministic check pass | The supervisor decides completion; here exit codes do, never an agent's claim — and `Bugs.md` #15 is the case where every self-reported signal agreed and all were wrong |
| `TOOL_CALL_RUN_LIMIT` · `MAX_WORKER_REPORTS` | Per-step attempt cap · fix-loop bound | Bounds a loop the model cannot exit (ADR-011); here the same bound must also distinguish "stopped because done" from "stopped because bounded" in what it records |
| `TEAM_CONFIG` per-level recursion budget | Budget ledger allocation | A sub-graph inherits the parent's allowance unless given its own (`Bugs.md` #12); the ledger must allocate per delegation, never inherit (`Research.md` topic 15) |
| `run_logs/<app>_<timestamp>.debug.log` | Event/Trace/Cost/Audit log | Written during the run, per-run correlation id, and it is what surfaced `Bugs.md` #15 at all; the real log must additionally survive process death and record *why* a run stopped (ADR-005, ADR-013) |
| `llm/invoker.py` FIFO gate + 429 handling | Budget ledger reactive signal · workspace lock | Detects HTTP 429s with structured headers; the ledger must detect process exits with configurable message strings (`Research.md` topic 11). Against this endpoint **both** its measurement paths read nothing, silently (`Bugs.md` #6a) |
| `claude_agents/cli.py` · `temp_agents/runners.py` | **Agent adapter** (Stage 4) | The closest correspondence in the sandbox, and the newest: both invoke a real vendor CLI, isolate its tool surface, classify its failures, and never raise past the caller. What they do not do is own a worktree, hold a lease, or survive their own death — an adapter must reconcile what a killed agent left behind (ADR-005), and a subprocess that simply returned an error never has to |
| `claude_agents/mcp_bridge.py` | Adapter's tool surface + child observability | Establishes that a vendor's tools attach *asynchronously* and its child's stderr is *captured, not forwarded* (`Bugs.md` #21, #22) — so an adapter must verify its tool surface before the first turn and route the child's diagnostics into the parent's audit trail |
| Claude Code's `total_cost_usd` · `session_id` per call | Budget ledger entry · Session Registry | The first readable consumption in this project, and it is **per vendor**: Codex reports none of it, so a two-agent ledger is fed by measurement for one and configured estimate for the other — the `degraded` state made concrete (`Research.md` topic 16) |
| `temp_agents/supervisor.py` concurrent dispatch | Parallel worktree execution | Two CLIs overlap for the cost of two threads, because the work is in the subprocesses; the real constraints are budget allocation and the workspace lock, not the dispatch mechanism (`Research.md` topic 18) |
| `main.py` deleting both target files before the run | Orphan reconciliation on restart | "The file contains X" and "*this run* produced X" are different questions, and the difference is one `unlink`; the orchestrator's version is reconciling plan state against actual Git state rather than assuming an interrupted run did nothing (ADR-005) |

The single decisive difference, and the reason the orchestrator cannot be a thin wrapper over a graph framework: in the sandbox the workers are constructed inside one Python process and hand control to each other cooperatively, whereas here they are separate vendor processes that can be killed mid-sentence by a rate limit. Everything ADR-005 exists to solve is what an in-process `Command` handoff never has to.

Two rows of that table are no longer purely aspirational, which is the substantive change of 2026-08-06: `claude_agents/` and `temp_agents/` do drive real vendor CLIs, so the adapter correspondence is now with running code rather than with a plan. The gap that remains is precisely the one ADR-005 names — those adapters return an error where a real one must reconcile a half-finished worktree — and it is worth stating plainly that the sandbox now contains the only code in this repository that invokes an agent CLI at all, while `orchestrator/` still holds one file that fails its own checks.

## Technology Stack

| Component | Technology (provisional) | Notes |
|---|---|---|
| External-agent execution/session runtime | **Maestro** (maestro-flow) v0.5.61, **installed repo-locally** | Chosen 2026-08-03 — see ADR-004. `maestro delegate` provides background delegation to a different backend coding-agent CLI mid-task, which is the primitive the earlier 10-tool survey found missing everywhere. Supersedes the Claw Orchestrator / OpenHands Agent Canvas question left open by ADR-002 and ADR-003. Two constraints carry forward: its built-in decision gates are all post-execution, so a pre-execution plan-review checkpoint must be assembled manually; and its own completion signals stay subordinate to this project's deterministic checks. Since 2026-08-04 it is a repo-local npm dependency at `node_modules/.bin/maestro` and is **not on `PATH`** (ADR-007) — every invocation, from orchestrator code included, must resolve it explicitly. As of 2026-08-06 the sandbox drives Claude Code 2.1.220 and codex-cli 0.146.0 as subprocesses **directly**, not through Maestro (ADR-014) — deliberately, since the nine CLI behaviours catalogued in `Research.md` topic 17 sit beneath `maestro delegate` too, and delegating through it inherits rather than avoids them. Its `--timeout` is documented as a *stale-stream* timeout, i.e. it does not bound a CLI that is streaming while making no progress, so a dispatcher needs its own wall-clock deadline over the subprocess (ADR-016). |
| Routing / supervisor / workflow | *Deferred* — LangGraph, Temporal, or hand-rolled | Maestro supplies a run/step lifecycle, so whether a separate workflow engine is needed underneath it is open — see Research topic 7. Langflow is ruled out as a runtime (it is a visual builder over LangChain, not a durable state machine); if a visual surface is wanted it belongs on top as an authoring/monitoring UI. Whatever is chosen, nodes wrap CLI subprocess calls (`claude -p ...`, `codex exec ...`) exactly like LLM-calling nodes — see Research topic 4. Hands-on input arrived 2026-08-05 (ADR-008): LangGraph's `Command`/`Literal` edge inference and per-invocation config are ergonomic for in-process routing, but it silently drops state updates to undeclared keys (`Bugs.md` #5a), propagates execution budgets into sub-graphs by default (`Bugs.md` #12), and offers nothing on the axis that actually decides this slot — durability across process death. |
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

### 2026-08-05 — The sandbox hierarchy runs end to end; four decisions come back from it, and one clean run that produced nothing

The target architecture above is unchanged, and the orchestrator track gained no code. What changed is that the sandbox (ADR-008) went from *compiles* to *runs*, which converted a set of documented intentions into measured facts — several of them uncomfortable ones. The Implementation Status section and the sandbox mapping table above are rewritten accordingly.

* **Both latent defects fired, as written.** `Bugs.md` #5 raised exactly its predicted traceback on first invocation. #6 degraded exactly as silently as its Symptom field warned — the header-derived cooldown floor was `0.0` on 9 of 9 calls in a single run, with no signal above DEBUG. A third defect in the same function was found only while fixing #5 and is worth carrying: **LangGraph silently discards state updates addressed to keys absent from the schema**, so the supervisor's `update={"next": goto}` had always been a no-op. An unknown edge target is a compile-time error; an unknown state key is not an error at all. The Canonical Task Context Store cannot inherit that behaviour — it is precisely a schema many writers update.
* **Measurement against this endpoint is not degraded, it is absent.** Beyond #6's cooldown, the TGI responses carry no usage figures at all (`token usage — input=n/a output=n/a total=n/a`, every call), so the *proactive accumulation* half of ADR-006 has no data source here either. Both halves of the ledger analogue read nothing, and both runs looked entirely normal. ADR-006's "state field" clause is now load-bearing rather than tidy: `available` computed from no data must be a distinct, visible ledger state carried on the entry, not a log line at the point of failure.
* **Protocol compatibility is per-feature and directional** (`Research.md` topic 12, ADR-010). A measured capability matrix for the local TGI 3.0 server: plain chat 200, `response_format` json_object/json_schema **500**, tool calling 200 — and `json_schema` is what the client library sends by default, so the default path was the one route the server does not implement. Three further deviations sit in the *responses*, plus one that only appears on a second turn. All are absorbed in a single `httpx` transport rather than at the call sites, because the first fix attempt patched the supervisor and would have left all five workers broken one turn later. This is the shape the Stage 4 **agent adapter** layer should take: vendor quirks normalize inside the adapter, once, never in the router or the workflow code downstream.
* **Termination is structural, not promptable** (`Research.md` topic 13, ADR-011). A controlled A/B established that the model is grammar-constrained to emit a tool call on every turn while tools are attached, so a ReAct loop's exit condition is unreachable — this is a property of the serving stack, not a defect in the agent code. Two prompt-level remedies were tried and *observed to fail*. What worked was caps in code at both the worker and supervisor layers. Two consequences for this design. The per-step attempt cap and fix-loop bound that the 2026-08-03 changelog listed as non-optional refinements are reclassified as **termination design**, in the same category as the workspace lock. And bounding a loop at one layer *relocated* it to the next rather than closing it — the guard's own terminal message was read downstream as unfinished work — so whatever ends a run must also state what the run accomplished.
* **The most important result is a run that succeeded by every available signal and did nothing.** `run_logs/agent_run_20260805_163729.debug.log`: five steps, no exception, exit 0, a `FINISH` decision, and `documents=none` against a request that asked for a file. The top-level supervisor routed to the research team twice and never to the writing team; the repeat cap then forced `END`. The research team's closing report even asserts the results *"can be compiled in a file named 'research_results.txt'"* — a completion claim for an action nothing performed. This is the concrete case behind two things this document already asserted on principle: completion is decided by deterministic checks and never by an agent's self-report, and Maestro's own completion signals stay subordinate to exit codes (ADR-004). It also adds a requirement to the Event/Trace/Cost/Audit log — **a stop must record why it happened**, since a bound firing and work completing are different outcomes that currently share one log line — and a caution for the v0 acceptance test: A1–A8 must be checked against produced artefacts, because a run can satisfy every internal success signal and still have accomplished nothing.
* **Budgets are inherited unless allocated** (`Research.md` topic 15). A sub-graph invoked without its own config draws on the parent's execution budget, so the level that exhausts an allowance is not the level that misconfigured it, and the traceback points away from the cause. Fixed here with explicit per-level allocation. For the budget ledger the requirement is the same: allocate **per delegation, never inherit** — `maestro delegate` spawns sub-work by design, and ADR-005's handoff packet itself consumes the receiving agent's allowance.
* **Two sandbox-scoped decisions** with orchestrator relevance: ADR-012 reorganized the sandbox into packages with a single config module and a separate prompts module, on the grounds that the constants *are* the findings — `TOOL_CALL_RUN_LIMIT = 3` with its reasoning attached is the only durable in-code record of `Bugs.md` #9. ADR-013 replaced rather than repaired the ported logging module, which had never executed (`Bugs.md` #14), and established the distinction ADR-009 lacked: port what is expensive to re-derive, rewrite what is cheap. The resulting per-run debug log is what surfaced the finding above; nothing on the console distinguished that run from a successful one.
* **Sandbox track security note, recorded because it will recur in the real system.** The document tools built paths with `os.path.join(cwd, "temp", file_name)`, which discards its base when the tail is absolute — so any absolute `file_name` the model chose escaped the workspace, and since the tools also *returned* absolute paths, the model echoed them straight back (`Bugs.md` #10). Path containment must be enforced by construction at the boundary, never by trusting a well-formed argument whose author is a language model. Unfixed by choice and confined to the sandbox: `tools/code.py` executes model-generated code in-process with no isolation.

---

### 2026-08-06 — The sandbox starts driving real agent CLIs, so the adapter layer stops being hypothetical; and the clean-run-that-produced-nothing is half closed

The target architecture above is unchanged and the orchestrator track still holds one file. What changed is which parts of this document are backed by running code: the sandbox now invokes Claude Code and Codex as subprocesses, which is the first time anything in this repository has driven an external coding agent, and the Implementation Status tree and mapping table are rewritten to say so.

* **`Bugs.md` #15 is partly closed, and the part that closed is the part that was a design defect.** The team-handoff half is fixed: a team is now handed the whole conversation rather than `messages[-1]`, so an agent invoked twice is no longer re-researching its own previous report and the writing team finally sees the file name it is supposed to produce; a team's report folds back everything it added rather than its closing message, which after a forced FINISH is often the thinnest. The routing half is **not** fixed and is not fixable at the code layer — it is an 8B model's judgement — which is exactly why the next point matters.
* **Two interchangeable agent implementations behind one graph** (ADR-014). Every supervisor and worker has a Claude Code counterpart selected by a config flag, with the same node name, state, `Command`, and report format; the graphs are untouched. The immediate return is diagnostic: the same graph, prompts, and request that had been terminating with `documents=none` routed research → writing → FINISH correctly and produced a grounded five-tool document on the CLI path. That is the cleanest evidence available that the topology is sound and the router's model is the constraint — a question the single-model sandbox could not have answered, and one the orchestrator will face constantly, since "wrong architecture" and "wrong agent for this step" are the two hypotheses its own routing decisions sit between.
* **The Stage 4 adapter layer now has a worked example and a measured cost.** `claude_agents/cli.py` and `temp_agents/runners.py` are adapters in everything but name: one place per vendor where the CLI is invoked, a result type that never raises at the boundary, and explicit isolation of the tool surface (`--tools ""` for built-ins, `--strict-mcp-config` to ignore every MCP server configured elsewhere on the machine). `Research.md` topic 17 catalogues nine behaviours a CLI has that a model API does not, and the summary is that **an agent CLI is a second agent runtime with its own context, tools, defaults and opinions**, all of which compete with the caller's. ADR-010 established that a vendor's *wire format* normalizes inside the adapter; this establishes that its *runtime behaviour* does too, and that the second category is larger and far less discoverable. These behaviours also sit beneath `maestro delegate`, so ADR-004's layer inherits rather than avoids them.
* **Consumption became readable, for the first time, and only from one vendor** (`Research.md` topic 16, ADR-006 status). Claude Code returns `total_cost_usd`, per-model token counts, `num_turns`, and `session_id` on every invocation; a measured five-step run cost **$1.05 across 9 calls**. Codex returns none of it and the local endpoint returns none of it. So a two-agent v0 ledger is populated by measurement for one agent and configured estimate for the other, which is the `degraded` state this document has argued for on principle since 2026-08-04, now with an instance. Two riders: the figure is *consumption*, never *remaining*, so ADR-006's precedence is untouched; and it includes the harness's own spend, so a ledger fed from it measures an agent-plus-its-runtime.
* **A worker that reported writing a file it had no tool to write** (`Bugs.md` #21). Claude Code attaches `--mcp-config` servers asynchronously and does not wait, so the first turn was planned against no tools at all; the report was fluent, specific, consistent with the request, and false. This is `Bugs.md` #15's failure mode recurring one substrate up, and it was caught the same way — by checking the filesystem. Two adapter requirements follow, both now stated in ADR-015: **verify the tool surface before the first turn**, and **route the child process's diagnostics into the parent's audit trail**, since a captured-not-forwarded stderr is what made the sibling defect (`Bugs.md` #22) undiagnosable until an env var closed the hole.
* **A limit must name its unit, and an adapter must not raise** (ADR-016). Three failures shared one shape: a per-read socket timeout that a server holding the connection open never trips (one turn ran 388 s), an executor whose `shutdown(wait=False)` still blocked at interpreter exit and left the process hanging after the run had printed its summary, and one worker's provider error aborting a graph that had already completed a team's work. The fixes are a wall-clock deadline over a whole turn on a daemon thread, and a choke point that converts any failure into a report the supervisor can route around. The sentence that generalizes: **a supervisor can route around a worker that failed; it cannot route around a traceback.** This lands on `maestro delegate --timeout` directly, which is documented as a *stale-stream* timeout — the same blind spot in the tool this project delegates through.
* **A rule loses to a fact** (ADR-011 status). Three more prompt-level constraints were observed losing to something concrete already in the model's context: the note taker aimed its outline at the deliverable because the request named that file; `doc_writer` kept issuing a second whole-file write; Claude Code wrote its chart to its own scratchpad because *its* system prompt named that path absolutely. Each was fixed at the tool boundary rather than in wording — middleware caps, refusal inside the tool with a self-correcting error, and the tool's **return value** used as an instruction channel, which an 8B model heeds where the same sentence in a system prompt did not. For this architecture the consequence is that the Handoff Builder's packet is not the only channel to a delegated agent; what its tools *return* is a second one, and on the evidence a more reliable one.
* **Parallel dispatch is cheap; verification is what needs designing** (`Research.md` topic 18). Two vendor CLIs run concurrently for the cost of two threads, because each is a `subprocess.run` blocked on a pipe — so the constraints on fan-out are budget allocation and the workspace lock, not the dispatch mechanism. The more useful half is the verification: the demo deletes both target files before starting, because "the file contains X" and "*this run* produced X" are different questions, and a stale artefact would have passed while the run wrote nothing. That is ADR-005's orphan-reconciliation requirement at artefact scale, applied preemptively rather than after the fact. It also produced a clean instance of why completion is checked deterministically: the supervising model read a file, understood it, approved it, and could not see the UTF-8 BOM in front of it — not unreliable, simply unable to observe the property being checked.

---
