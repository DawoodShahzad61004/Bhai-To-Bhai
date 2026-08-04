## Chronological Log

### July 2026 — Defining requirements and surveying existing orchestration tools

- **Wrote a 35-item requirements checklist:** covering multi-CLI agent support (Claude Code, Codex, Gemini CLI, OpenCode/Aider), dynamic per-stage agent selection, mid-task switching, planner→implementer→reviewer→fixer workflows, canonical cross-agent task context, native session resumption, deterministic (non-LLM-judge) completion checks, human approval gates, and honest handling of the fact that hidden model state cannot cross vendors.
- **Surveyed 10 candidate orchestration frameworks against the checklist:** Maestro Orchestrate, Claw Orchestrator, Codex Orchestrator, The Pair, Sandbox Agent, AgentOS, Agent Orchestrator, claude-codex-gemini, Session Orchestrator, CLI Continues — see `Research.md` topic 2. None fully satisfied the checklist.
- **Evaluated two more candidates, Command Code and OpenHands Agent Canvas:** ruled out Command Code (proprietary, not a cross-vendor orchestrator); found OpenHands Agent Canvas closer but still missing dynamic cross-agent routing and deterministic completion checks — see `Research.md` topic 3.
- **Adopted the 5-file markdown tracking system** (`Status.md`, `Architecture.md`, `Decisions.md`, `Research.md`, `Bugs.md`) used previously in `RAG-work`, per `Markdown-Tracking-Guide.md`.

---

#### 2026-07-31 — Requirements checklist, tool survey, and initial architecture decisions

* Set out to find — or determine whether it's necessary to build — an open-source, locally deployable platform that dynamically orchestrates existing coding-agent CLIs (Claude Code, Codex, Gemini CLI, others) through planner → implementer → reviewer → fixer workflows with a shared, canonical task context across handoffs.
* Diagnosed: no single surveyed tool (10 frameworks, plus Command Code and OpenHands Agent Canvas) fully satisfies the requirements checklist. The recurring gap across every candidate is that "multi-CLI execution/session runtime" and "dynamic cross-agent routing" are always separate concerns — no tool merges both, and none uses purely deterministic (exit-code-based) completion checks.
* Also worked out, separately, how to integrate CLI-based coding agents as LangGraph nodes (subprocess wrapping) and how to distribute a shared skill file (e.g. `graphify`) to multiple agent CLIs reliably — landed on explicit prompt injection over relying on each CLI's own auto-discovery.
* Decided (provisionally): (1) explicit skill injection over CLI auto-discovery (ADR-001); (2) target stack of Claw Orchestrator + LangGraph + MongoDB + Git worktrees + Phoenix/OpenTelemetry, reusing prior project experience rather than combining several full orchestration platforms (ADR-002); (3) keep OpenHands Agent Canvas open as an alternative to Claw Orchestrator for the execution/session layer specifically, pending hands-on evaluation (ADR-003).
* No code was written this session — this was a research and documentation session. Documented the target architecture in `Architecture.md` based on the above.
* Tracked in: `docs/Architecture.md` (initial target architecture + changelog entry), `docs/Decisions.md` (ADR-001, ADR-002, ADR-003), `docs/Research.md` (topics 1–4); no `docs/Bugs.md` entries (no code yet).

---

### August 2026 — Settling the execution layer and specifying the mechanics that carry the failover feature

- **Closed the execution-layer question:** evaluated OpenHands and Claw Orchestrator hands-on rather than from documentation, reviewed Temporal for the durable-workflow slot, then adopted Maestro after Maestro-flow and Flow-next surfaced — see `Research.md` topics 5, 7, 8 and ADR-004.
- **Specified the two mechanics the headline feature depends on:** handoff-by-reconstruction (ADR-005) and the budget ledger with reactive limit detection (ADR-006).
- **First `Bugs.md` entries recorded** — both environment/tooling failures encountered during evaluation, not application defects.

---

#### 2026-08-03 — Maestro chosen as the execution layer; handoff and budget mechanics pinned down

* Set out to settle ADR-003's open two-way choice between Claw Orchestrator and OpenHands Agent Canvas by working with them directly instead of comparing feature lists.
* Diagnosed: the two are not really competing on the same axis. Claw's abstraction is a coordinated *group* of external agent sessions (Council, fan-out, worktree-backed parallelism); OpenHands' is a complete *environment* for running dev agents (sandboxes, UI, secrets, ACP, deployment backends). Neither implements token-aware cross-agent failover, so the core custom work is identical either way — and if the orchestration logic lives in a separate layer, Claw's coordination primitives go unused and its advantage collapses. Also corrected an earlier misreading: OpenHands *does* provide persistent conversations and multi-engine access via ACP (`Research.md` topic 5).
* Attempted a hands-on OpenHands trial against a self-hosted OpenAI-compatible LLM endpoint, with a first task chosen to exercise file creation plus deterministic verification without needing the rest of the stack running. It never reached agent execution — every request failed with `litellm.InternalServerError: OpenAIException - Connection error.` Cause not isolated; the trial was dropped when the execution layer moved to Maestro the same day (`Bugs.md` #1).
* Reviewed **Temporal** as the durable-workflow option alongside the Langflow/LangGraph question. Flagged that the provisional stack table said Langflow while ADR-002 had chosen LangGraph — these are different tools, and Langflow is a visual builder rather than a durable state-machine runtime, so it is ruled out as the engine. Detailed Temporal evaluation notes were not captured in the session transcripts and are still owed to `Research.md` topic 7.
* Learned about **Maestro-flow (Ralph)** and **Flow-next**, and found that both already do mid-task delegation to a *different* coding-agent CLI — the exact primitive the earlier 10-tool survey concluded was missing everywhere. Ralph's `cli` node type routes a chain step through `maestro delegate` as a background delegate execution; Flow-next exposes `plan-review` and `impl-review` as named commands with a swappable review backend. Ralph's decision gates are all post-execution, so a pre-execution plan-review checkpoint needs hand-assembly; Flow-next covers that natively (`Research.md` topic 8).
* **Decided: use Maestro** (ADR-004) — chosen for its general-purpose `delegate` primitive, Session/Run lifecycle, and knowledge system, and because it is already installed and configured here. Flow-next is the more configurable of the two out of the box and is retained as the fallback. This withdraws Claw Orchestrator from ADR-002 and resolves ADR-003 in the negative for both of its candidates.
* Also decided two mechanics that everything else rests on: handoff packets are **reconstructed on entry** from continuously-written durable artifacts rather than summarized on exit (ADR-005) — the naive summarize-on-exit design fails precisely when an agent is killed by a rate limit, which is the case the whole feature exists for. And agent budgets are modelled as a **configured ledger with reactive limit detection as the authoritative signal** (ADR-006), because no target agent exposes remaining allowance and the four are not bounded by comparable quantities.
* Environment work: installed Claude Code v2.1.220 and fixed its PATH (`Bugs.md` #2); created a project virtualenv on **Python 3.13.13** via `uv`, moving off the locally installed 3.14 because the agent/LLM package ecosystem lags new interpreter releases and several intended dependencies ship compiled wheels.
* No code was written this session. Build guidance and a phase-scoped dependency list were produced during the session but **were not saved into the repository** — `docs/Build-Guide.md`, `requirements.txt`, and `requirements-dev.txt` do not exist in the working tree or in git history. Their substance is preserved in ADR-005, ADR-006, `Research.md` topics 6–8, and the `Architecture.md` changelog; the phased build order and the concrete v0 acceptance test are **not** yet recorded anywhere and should be rewritten before implementation starts.
* Target for v0, worth restating since it is the clearest definition of done: give the system a real task in a real repo, set one agent's budget artificially low so it will be exhausted partway through, and have the system plan, start implementing, detect the limit, fail over to a second agent mid-task, continue from the existing partial work rather than restarting, pass the deterministic check runner, and leave a complete audit trail.
* Tracked in: `docs/Decisions.md` (ADR-004, ADR-005, ADR-006; status updates on ADR-002 and ADR-003), `docs/Research.md` (topics 5–8), `docs/Architecture.md` (stack table, Agent Router, Handoff Builder, changelog entry), `docs/Bugs.md` (entries 1–2).

---
