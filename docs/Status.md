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
