# Build Guide

Phased implementation order for Bhai-To-Bhai, written 2026-08-04. This replaces the build guidance produced on 2026-08-03 that was never saved into the repository (`Status.md`, 2026-08-03, final bullets). It records the two things that entry flagged as missing everywhere: **the phased build order** and **the concrete v0 acceptance test**.

Nothing here overrides `Decisions.md`. Where a stage exists because of an ADR, the ADR is cited and remains authoritative.

---

## How this project grows

The build order below follows the working pattern from the sibling `RAG-work` project (`RAG-work/docs/Status.md`, April–July 2026), which took a system from nothing to a two-implementation, three-tracing-backend, GPU-microservice architecture without ever having a "big rewrite" phase. Six habits carried that:

1. **Infrastructure before intelligence.** April 2026 built `ingest.py`, `embedding_manager.py`, `vector_store.py`, `retriever.py` — and a plain single-pass `query.py` — *before* the agentic loop existed. The clever part was built on a data layer that already worked.
2. **A walking skeleton that runs end-to-end on real data, early.** `query.py` (non-agentic) preceded `agent_query.py` (4-phase loop). The first dry runs immediately surfaced the two problems that dictated the next month of work (acronym ambiguity, premature tool calls). Failures found by running set the backlog; speculation did not.
3. **Cheap storage first, real storage later.** Interaction history and thumbdowns were JSONL flat files from May until the MongoDB migration on 2026-06-27. The schema was learned by using it, then migrated once.
4. **One focused change per session, logged the same day.** Each dated entry is a single capability plus the bugs it exposed, with ADR and BUG identifiers. Nothing accumulates unrecorded.
5. **Don't adopt a framework until the hand-rolled version shows you what you need.** LangGraph was "researched, not adopted" on 2026-06-15, sandboxed on 06-16/06-17, and only then became a real implementation — built *alongside* the original, sharing one store, and benchmarked head-to-head rather than replacing it.
6. **Observability and optional services come after the core works.** Tracing backends arrived in July; the isolated GPU Marker service arrived on 07-24, once ingestion was stable enough to be worth accelerating.

Applied here, that means: **the durable artifact layer and a one-agent spine come before routing, budgets, or failover** — even though failover is the headline feature. This is also what ADR-005 already concluded independently ("load-bearing … and the first thing to build").

A second framing, from `Architecture.md`, governs every stage: this is *structurally a job scheduler whose workers are unreliable, expensive, and non-deterministic*. When a stage feels like it is about prompting, it has probably been scoped wrong.

---

## Target: the v0 acceptance test

Restated verbatim in substance from `Status.md` (2026-08-03), because it is the clearest definition of done and was not recorded anywhere else:

> Give the system a real task in a real repo, set one agent's budget artificially low so it will be exhausted partway through, and have the system plan, start implementing, detect the limit, fail over to a second agent mid-task, continue from the existing partial work rather than restarting, pass the deterministic check runner, and leave a complete audit trail.

Broken into the assertions a test can actually make:

| #  | Assertion                                                                            | Verified by                                                                                                   |
| -- | ------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------- |
| A1 | A plan exists with discrete steps before implementation begins                       | Plan document persisted, steps in`pending` state                                                            |
| A2 | Agent 1 starts work and completes at least one step                                  | ≥1 commit in the worktree attributable to agent 1; ≥1 step`done`                                          |
| A3 | Agent 1's exhaustion is detected, not guessed                                        | Ledger state transitions to`exhausted` from a non-zero exit + rate-limit match, not from the estimate alone |
| A4 | A handoff packet is built **without** agent 1 having written anything on exit | Packet assembled purely from commits, step status, event log, last check result                               |
| A5 | Agent 2 continues rather than restarts                                               | Agent 2's first commit builds on agent 1's HEAD; no step already`done` is re-executed                       |
| A6 | Dead ends are carried forward                                                        | The packet contains agent 1's failed attempts; agent 2 does not repeat the identical failing approach         |
| A7 | Completion is decided by exit codes, not by any agent's claim                        | Check runner result recorded with the commit SHA it ran against                                               |
| A8 | The whole run is reconstructable after the fact                                      | Event log replays the run start-to-finish, including the switch and its reason                                |

**A4 is the test.** A1–A3 and A5–A8 can all pass in a design that still fails the moment a real rate limit kills a process mid-sentence. Kill agent 1 with `SIGKILL` rather than a graceful stop when exercising this.

---

## Stage 0 — Preflight and repository skeleton

**Goal:** the environment is provably capable of what later stages assume, and failures are loud at startup rather than opaque later.

**Why first:** `Bugs.md` #2's Prevention note asks for exactly this — the orchestrator invokes every agent as a subprocess, so each CLI must resolve from *the orchestrator's* environment, not from an interactive shell where PATH was fixed by hand. `Bugs.md` #1 adds the network variant: verify an endpoint from the process that will actually call it.

**Verified present on this machine as of 2026-08-04:**

| Component   | Status                                              |
| ----------- | --------------------------------------------------- |
| Python      | 3.13.13 (`.venv`, per `Status.md` 2026-08-03)   |
| Maestro     | 0.5.61                                              |
| Claude Code | 2.1.220                                             |
| Codex       | codex-cli 0.146.0                                   |
| Git         | 2.52.0.windows.1                                    |
| Docker      | 29.6.2                                              |
| MongoDB     | listening on`localhost:27017` (`mongosh` 2.8.3) |

**Not present:** `gemini`, `opencode`, `aider` are not on PATH. Also note `~/.maestro/cli-tools.json` currently has **only `codex` enabled** (`claude`, `gemini`, `opencode`, `agy` are all `enabled: false`). Delegating to Claude Code requires flipping `claude.enabled` to `true` there. Plan v0 around **Codex + Claude Code**, the two agents that actually exist here; treat Gemini/OpenCode/Aider as Stage 8 adapters.

**Build:**

- `preflight.py` — resolves every configured agent binary, runs each one's version probe, checks MongoDB connectivity, checks `git worktree` support, and exits non-zero with a per-item report. Nothing else runs until this passes.
- `requirements.txt` / `requirements-dev.txt` — these do not exist in the working tree or in git history and must be rewritten. Keep them phase-scoped: Stage 0–3 needs little more than `pymongo`, `pydantic`, and a test runner. Do not pre-install the Phoenix/OTel stack; it belongs to Stage 9.
- Repository layout, created empty and filled by later stages:

```
orchestrator/
├── preflight.py          # Stage 0
├── config.py             # Stage 0 — constants, feature flags, vendor error strings
├── store/                # Stage 1 — MongoDB access, event log, run/step records
├── workspace/            # Stage 1 — git worktree lifecycle + lock
├── checks/               # Stage 2 — deterministic check runner
├── adapters/             # Stage 4 — one module per agent CLI
├── handoff/              # Stage 3 — packet reconstruction
├── ledger/               # Stage 5 — budget accounting
├── router/               # Stage 6 — agent selection
└── skills/<name>/SKILL.md  # ADR-001 — canonical skill source, injected verbatim
```

**Done when:** `python preflight.py` passes on a clean shell, and fails loudly and specifically when an agent binary is renamed away.

---

## Stage 1 — The durable artifact layer

**Goal:** four artifact classes are written *continuously during* a run, such that killing the process at any instant still leaves a recoverable state.

**Why now:** ADR-005 names this the first thing to build. Every later stage reads from it, and it is the only stage whose absence cannot be worked around later — retrofitting durability onto a system that buffers is a rewrite.

**Build the four artifacts from ADR-005 exactly:**

| Artifact          | Requirement                                                                                         | Failure if skipped                                    |
| ----------------- | --------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| Worktree commits  | Agent instructed to commit per logical step, **plus** an orchestrator-side auto-commit timer | A killed process leaves no diff                       |
| Plan step status  | `pending` / `in_progress` / `done`, persisted on every transition                             | Cannot tell what was finished from what was attempted |
| Event stream      | Written to MongoDB**as each event arrives**, never buffered to process exit                   | The exact failure mode ADR-005 exists to avoid        |
| Last check result | Tied to the commit SHA it ran against                                                               | A pass/fail with no anchor is not evidence            |

Also build here, because they are storage concerns:

- **Worktree manager** with a **workspace lock — one active agent per worktree.** `Architecture.md` (2026-08-03 changelog) names the absence of this as one of two failure modes that "burn every agent budget at once." It is not a refinement.
- **Crash reconciliation on startup.** ADR-005's second consequence: a run with a start timestamp and no end timestamp is an orphan. Restart must reconcile persisted plan state against *actual Git state*, never assume an interrupted run did nothing.

**Done when:** you can start a long-running write to a worktree, `SIGKILL` the orchestrator, restart it, and have it correctly report which steps completed, what the diff is, and that the run was orphaned — with no summary having been written by anything.

**Test this stage adversarially.** Kill at: mid-commit, between step transition and event write, and during check execution.

---

## Stage 2 — Single-agent end-to-end spine

**Goal:** one agent, one task, one worktree, real repo, start to finish, with a deterministic verdict. No routing. No failover. No budget.

**Why now:** this is `RAG-work`'s `query.py` — the walking skeleton that proves the plumbing before the interesting behaviour is layered on. `Status.md` (2026-08-03) already sequences it this way: "a single-agent end-to-end spine first."

**Build:**

- **Task intake → plan → execute → check**, driven through `maestro delegate` against **Codex** (the one agent enabled in `cli-tools.json` today).
- **Deterministic check runner** (`checks/`): reads a verification manifest, runs each check as a subprocess, decides pass/fail **from exit codes only**. Not an LLM judge — ADR-003 is explicit that this is why `/goal`-style completion was rejected. Record each result against a commit SHA (Stage 1).
- **Explicit skill injection** per ADR-001: `load_skill(name)` reads `skills/<name>/SKILL.md` and prepends it to the prompt string on every call, for every agent. Do not rely on `.claude/skills/` or `AGENTS.md` auto-discovery — in headless subprocess mode there is no way to confirm it fired.

**Two Maestro constraints to encode now, both from ADR-004:**

- Maestro's built-in decision gates are **all post-execution**. There is no built-in gate that reviews a plan *before* execution. If you want a plan-review checkpoint, assemble it manually with a `maestro delegate` call — do not wait for a gate that does not exist.
- Maestro's own completion signals are **subordinate to the check runner**. A Maestro run reporting success is an input, not a verdict.

Useful Maestro surface for this stage (see `maestro-flow documentation/delegate-async-guide.md`): `maestro delegate "<prompt>" --to codex --mode write --async` returns an execId immediately; `status`, `output`, `tail`, and `cancel` operate on it; `--timeout <ms>` sets the stale-stream kill (default 600000ms), and per-tool `streamTimeoutMs` in `cli-tools.json` does the same globally.

**Done when:** a real task in a real repo runs unattended and produces a pass or fail that you trust because you can see which command exited non-zero.

**Then run it on something real, immediately.** This is habit 2. Whatever breaks here sets the Stage 3–6 backlog, and it will not be what you predicted.

---

## Stage 3 — Handoff Builder (reconstruct on entry)

**Goal:** given only Stage 1's artifacts, produce the packet an incoming agent receives — with no cooperation from the outgoing agent.

**Build** the packet contents from `Architecture.md`'s Handoff Builder section: canonical objective and constraints, relevant prior decisions, current Git diff, latest check failures, remaining work, the agent's own native session reference if resuming, and injected skill content.

**Three constraints that are easy to under-build:**

1. **Reconstructed at entry, never summarized at exit** (ADR-005). Enforce it structurally: the Handoff Builder must not have an API that an outgoing agent can call.
2. **Dead ends carried explicitly** — "X was tried and fails because Y." Without this the incoming agent reattempts exactly what killed its predecessor. This is acceptance assertion A6, and it is the most commonly omitted field.
3. **The packet is itself budgeted and measured.** Every handoff consumes the receiving agent's allowance — the very resource being managed. Record packet size per handoff from day one; you will need the number in Stage 6 to price switching.

Keep the **Session Registry** separate from the **Canonical Task Context Store** (`Architecture.md`, and Research topic 1): native session IDs are per-vendor bookkeeping for resuming the *same* agent, not a cross-vendor continuity mechanism. Conflating them reintroduces the false claim the project explicitly refuses to make.

**Done when:** you can kill agent 1 with `SIGKILL` mid-step and produce a packet that a human reading it could use to continue the work correctly.

---

## Stage 4 — Adapter interface and the second agent

**Goal:** agent-specific behaviour lives behind one interface, and a second real agent runs through it.

**Why now:** `Status.md` sequences it here ("then the adapter interface and a second agent"), and it is the earliest point where the interface is informed by a working implementation rather than guessed.

**Build:**

- `AgentAdapter` covering: invocation, streamed event normalization, usage extraction from each call, native session capture/resume, and **rate-limit classification**.
- **Claude Code adapter** as the second implementation. Requires setting `claude.enabled = true` in `~/.maestro/cli-tools.json`.
- Keep the adapter boundary above Maestro, not below it. ADR-004 retains Flow-next as the fallback if Maestro's post-execution-only gates prove too restrictive; that fallback is only real if swapping the execution layer does not mean rewriting the adapters.

**Done when:** the Stage 2 spine runs identically against Codex and Claude Code with only a config change, and both produce normalized events and usage records.

---

## Stage 5 — Budget ledger

**Goal:** a normalized per-agent allowance model that is honest about not being queryable.

**Build exactly what ADR-006 specifies** — this stage has more ways to be subtly wrong than any other:

| Element              | Rule                                                                                                                          |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `limit_type`       | `rolling window` / `daily requests` / `monetary` / `none` — the four agents are not bounded by comparable quantities |
| Configured limit     | User-supplied. No vendor exposes remaining allowance (Research topic 6)                                                       |
| Consumption          | Accumulated from per-call reported usage —**an estimate**, used for pre-emptive routing only                           |
| Exhaustion           | A non-zero exit carrying a rate-limit message —**authoritative**, sets cooldown and triggers failover                  |
| State                | `available` / `degraded` / `exhausted` / `cooldown`                                                                   |
| Vendor error strings | **In configuration, not code.** Vendors reword them                                                                     |

The precedence is the whole point: the estimate routes, the reactive signal decides. Do not let drift in the estimate become a wrong routing decision instead of a correct failover.

Two agent-specific realities to encode: Gemini CLI's free tier is bounded by **request count**, not tokens, so a token-shaped ledger cannot represent it. DeepSeek is a *model*, not an agentic coding CLI — it is either wrapped in a harness accepting an OpenAI-compatible endpoint (Aider, OpenCode) or restricted to non-agentic roles. Its ledger placement stays unresolved until that is settled hands-on.

**Done when:** you can artificially set a limit low, run until it trips, and see the ledger transition to `exhausted` from the reactive signal — with the estimate visibly disagreeing beforehand, which is expected and fine.

---

## Stage 6 — Agent Router and the failover loop

**Goal:** the headline behaviour, assembled from parts that all already work.

**Build:**

- **Deterministic rules first**, per `Architecture.md`: architecture/planning → Claude Code, implementation → Codex, large-context repo analysis → a large-context agent, mechanical refactors → OpenCode/Aider. An LLM router is a fallback for ambiguous cases only, not the default path.
- **Reserve headroom before dispatch** (ADR-006). A mid-task death costs both the partial work and a cold-start handoff — do not dispatch a job that visibly will not fit.
- **Price the cost of switching** (ADR-006). Resuming an agent that still holds a native session is materially cheaper than cold-starting a different vendor. Use the packet-size measurements from Stage 3.
- **Per-step attempt cap that escalates to a different agent.** The second of the two failure modes named in `Architecture.md`'s 2026-08-03 changelog: without it, unbounded fix loops burn every agent budget at once. Cap attempts per step, then change agent — do not retry the same agent indefinitely.

**Done when:** the loop runs planner → implementer → check → fixer with a real switch in the middle, driven by ledger state rather than by a hardcoded sequence.

---

## Stage 7 — v0 acceptance

Run the scenario from the top of this document against assertions A1–A8. Use a real repository and a real task, not a fixture. Kill the first agent with `SIGKILL`, not a graceful stop.

Record the result in `Status.md` as a dated session entry, and open `Bugs.md` records for every assertion that fails. If A4 fails, the fix belongs in Stage 1 or Stage 3 — not in the router.

---

## Stage 8 and beyond — after v0

Ordered by dependency, not by appeal. None of this should start before A1–A8 pass.

| Stage | Work                                     | Notes                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ----- | ---------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 8     | Reviewer stage and additional adapters   | Completes planner → implementer → reviewer → fixer.`Architecture.md` requires the reviewer to be a *different* agent than the implementer. Gemini CLI, OpenCode/Aider adapters land here — none is currently installed                                                                                                                                                                                             |
| 9     | Observability — Phoenix / OpenTelemetry | Deliberately deferred, per habit 6. The event log from Stage 1 already carries the audit trail; this is for traces, durations, and cost attribution                                                                                                                                                                                                                                                                        |
| 10    | Human approval gate                      | Optional, policy-driven, independent of which agents ran                                                                                                                                                                                                                                                                                                                                                                   |
| 11    | Workflow-engine decision                 | Research topic 7 is still open, and deliberately so. Maestro supplies its own run/step lifecycle, so decide**after** Stages 1–7 have shown what actually needs durable execution. Langflow is ruled out as a runtime; LangGraph and Temporal remain candidates for whatever executes inside Maestro's lifecycle. Adopting a framework before the adapter/handoff/ledger layers exist risks encoding the wrong shape |

Two documentation debts to clear while working: the Temporal evaluation notes owed to `Research.md` topic 7 (`Status.md`, 2026-08-03), and DeepSeek's ledger placement (ADR-006).

---

## Working rhythm

Carried from `RAG-work`, and the reason its documentation survived seven months of daily change:

- **One focused change per session**, logged in `Status.md` the same day with what was attempted, what was diagnosed, what was decided, and where it was tracked.
- **ADR on every real decision**, numbered, with options considered — including the ones rejected. `Decisions.md` here already supersedes ADRs in place (ADR-002 → ADR-004) rather than deleting them; keep doing that.
- **Bugs get identifiers and stay recorded after they are closed.** `Bugs.md` entries carry a Prevention field; both existing entries produced concrete build requirements in Stage 0 of this guide.
- **Update `Architecture.md`'s changelog when the shape changes**, not when the code changes.
- **Benchmark before and after any change you expect to matter.** `RAG-work` ran a 10-query benchmark before adopting its second implementation and a 15-batch/100-scenario benchmark after; both changed decisions.

---
