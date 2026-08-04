## 1. Cross-agent context transfer: what can and can't move between coding-agent CLIs

| Field | Detail |
|---|---|
| **Topic** | Whether "session context" can literally move between Claude Code, Codex, Gemini CLI, and similar tools when an orchestrator hands a task from one to another |
| **Date** | 2026-07-31 |
| **Findings** | A model's internal session state (KV cache, hidden activations, provider-side conversation state) cannot be transferred across vendors — there is no API that exposes or accepts it. What *can* be carried forward: (1) each CLI's own native resumable session ID, useful only for resuming with the *same* vendor later; (2) structured handoff summaries written by the outgoing agent; (3) shared Git state (diffs, commits, branches, worktrees); (4) a canonical task record (objective, requirements, decisions, completed/remaining work, failed attempts); (5) event/transcript history stored by the orchestrator itself, not by any vendor. |
| **Conclusion** | Any orchestrator design must explicitly build and pass a context packet at every handoff rather than assuming continuity. Native session IDs are an optimization for same-agent resumption, not a cross-vendor continuity mechanism. This is now a hard requirement in the project checklist ("does not falsely claim to transfer hidden token-level state between vendors") and shapes the canonical task-context schema in [[001-skill-distribution-and-explicit-injection]]-adjacent design work. |
| **Relevance to Project** | Governs the design of the canonical task context object and the "Handoff Builder" component described in `Architecture.md`. Also the reason a "Session Registry" (per-agent native session IDs) is a separate concern from the canonical task context. |

---

## 2. Survey of existing cross-agent orchestration frameworks

| Field | Detail |
|---|---|
| **Topic** | Whether an existing open-source (or otherwise) library already does what this project wants: dynamically alternate between Claude Code, Codex, Gemini CLI, and other coding-agent CLIs on a single task while maintaining shared context |
| **Date** | 2026-07-31 |
| **Findings** | Ten candidates were compared against a 35-item requirements checklist (see `Bugs.md`/checklist is reproduced in `Decisions.md` ADR-002 context). Summary fit scores: Maestro Orchestrate 9/10, Claw Orchestrator 9/10, Codex Orchestrator 9/10, The Pair 8.5/10, Sandbox Agent 8.5/10, AgentOS 8/10, Agent Orchestrator 8/10, claude-codex-gemini 7.5/10, Session Orchestrator 7/10, CLI Continues 7/10. No candidate scored a full match. Maestro Orchestrate is the closest complete, opinionated framework (Apache-2.0, persistent sessions, role assignment, Claude/Codex/Gemini/Qwen support) but is prescriptive about workflows. Claw Orchestrator is the strongest neutral multi-CLI runtime (named persistent sessions across Claude/Codex/Gemini/Cursor/custom CLIs, programmable routing) but leaves routing logic to the integrator. Sandbox Agent normalizes sessions/events/permissions across Claude Code, Codex, OpenCode, Cursor, Amp, and Pi, but explicitly does not decide agent selection itself — routing has to be built on top (e.g., with LangGraph). |
| **Conclusion** | No off-the-shelf tool fully satisfies the checklist; search continues. The most promising path is composing a neutral multi-CLI runtime (Claw Orchestrator or Sandbox Agent) with a hand-built routing/supervisor layer (LangGraph), rather than adopting a single fully opinionated platform. See ADR-002 for the resulting provisional stack decision. |
| **Relevance to Project** | Directly informs the "Agent Router" and the choice of external-agent execution layer in `Architecture.md`. |

---

## 3. Command Code and OpenHands Agent Canvas checklist comparison

| Field | Detail |
|---|---|
| **Topic** | Two additional, more recently surfaced candidates — CommandCodeAI/command-code and OpenHands/openhands (Agent Canvas) — evaluated line-by-line against the full requirements checklist |
| **Date** | 2026-07-31 |
| **Findings** | Command Code is a standalone proprietary coding agent, not a cross-vendor orchestrator — it does not support Claude Code, Codex, or Gemini CLI as external agents, and its public repository contains largely documentation with no visible OSS license or implementation, so it cannot be treated as open source. OpenHands Agent Canvas is a materially stronger candidate: it runs OpenHands, Claude Code, Codex, and Gemini CLI plus other ACP-compatible agents, provides persisted/branching conversations, ships official Docker and Windows support, and is MIT-licensed. Its key gap is that a "conversation" is scoped to one manually selected agent profile — it does not itself inspect a task, dynamically choose Claude for planning vs. Codex for implementation, then automatically hand off and later resume the first agent. Its `/goal` completion signal is an LLM judge, not the deterministic exit-code checks the project requires. |
| **Conclusion** | Neither tool is sufficient standalone. Recommendation captured as ADR-003: use OpenHands Agent Canvas as the external-agent execution/session layer candidate (potentially replacing Claw Orchestrator's role here) while keeping a hand-built LangGraph layer as the actual routing/supervisor — the same shape of solution as ADR-002, with OpenHands as an alternative execution-layer option under evaluation rather than a final replacement. |
| **Relevance to Project** | Reinforces that "provides an execution/session runtime for multiple CLIs" and "dynamically routes between them" are two separable layers in every candidate surveyed so far — none merges both. This separation is now treated as a structural assumption in `Architecture.md`. |

---

## 4. LangGraph as the routing/supervisor layer over CLI-based coding agents

| Field | Detail |
|---|---|
| **Topic** | How to integrate CLI-based coding agents (Claude Code, Codex) as nodes in an existing LangGraph `StateGraph`, and how to distribute a shared skill (e.g., a `graphify`-style SKILL.md) to multiple agent CLIs consistently |
| **Date** | 2026-07-31 |
| **Findings** | A LangGraph node can wrap a CLI call directly: shell out via `subprocess.run([...])` with a headless/non-interactive flag (`claude -p ... --output-format json`, `codex exec ... --json`), parse the JSON result, and return it as graph state exactly like an LLM-calling node would (`(state) -> dict`). GitHub Copilot has no scriptable agentic-loop CLI equivalent to Claude Code/Codex, so it's better modeled as a tool call inside a node (parse `gh copilot suggest` output and feed it back into state) rather than as its own graph node that owns a task end-to-end. For shared skills: Claude Code auto-discovers `.claude/skills/`, Codex has no equivalent and instead reads `AGENTS.md`; the reliable pattern is one canonical `skills/<name>/SKILL.md`, referenced by a symlink for Claude Code and a pointer line in `AGENTS.md` for Codex — but auto-discovery should not be trusted for headless/one-shot subprocess calls, since there is no visibility into whether it fired. The robust approach is to read the skill file and inject its contents directly into the constructed prompt string before the subprocess call, for every agent, every time. |
| **Conclusion** | Skill distribution to CLI-based agents in this project should default to explicit injection at prompt-construction time, with the symlink/`AGENTS.md` convention kept only as a fallback for humans running an agent interactively outside the orchestrator. See ADR-001. |
| **Relevance to Project** | Directly shapes the design of both the "Agent Router" nodes and the "Handoff Builder" in `Architecture.md`, and the decision recorded in ADR-001. |

---

## 5. OpenHands Agent Canvas vs. Claw Orchestrator — what each actually provides beneath custom orchestration logic

| Field | Detail |
|---|---|
| **Topic** | Following ADR-003, a deeper comparison of the two execution-layer candidates, specifically: given that the project's distinctive features must be hand-built either way, what does each platform actually save? |
| **Date** | 2026-08-03 |
| **Findings** | An earlier assessment (topic 3) understated OpenHands: it *does* provide persistent conversation storage (messages, events, files, task state, provided the backend storage/volume is persisted) and *does* provide multi-engine access to Claude Code, Codex, Gemini CLI, and other agents via Agent Client Protocol (ACP). The real difference is one of abstraction, not feature count. **Claw Orchestrator's** primary abstraction is a coordinated *group* of external coding-agent sessions: it ships fan-out, mixed-agent councils, consensus rounds, and Planner→Coder→Reviewer loops, with Git worktrees and branches integrated into those Council workflows, plus direct fine-grained control over supported coding CLIs and their sessions. **OpenHands'** primary abstraction is a complete *environment* in which development agents run: secure sandboxes, file editing and shell execution, repository integration, event streaming, Docker/VM/Kubernetes/local/remote execution backends, secrets handling, a monitoring UI, its own software-development agent, and scheduled/event-driven automations. Neither implements the project's headline behaviour — monitor an agent's remaining allowance, detect exhaustion or an unexpected stop, select a replacement, build a handoff, resume, and loop until deterministic checks pass. Also clarified: switching an LLM profile mid-conversation (which OpenHands supports while preserving history) is *not* the same as switching a coding-agent runtime — Claude Code → Codex is a change of agent process, not a change of model. |
| **Conclusion** | Neither platform reduces the custom work; they differ only in which surrounding layer you inherit. Claw is the better fit if multi-agent coordination primitives and direct CLI session control are the foundation you want; OpenHands is better if sandbox infrastructure, UI, integrations, and deployment management are worth more than coordination primitives. Critically, if the orchestration logic is built in a workflow layer anyway and Claw's Council/fan-out/Autoloop/worktree features go unused, Claw's advantage collapses to near zero — which reframes the question as "do we need an execution *platform* at all, or just a process supervisor?" A hands-on OpenHands trial was attempted the same day to settle this empirically; it was blocked by a connection failure to the local LLM endpoint before any agent work ran (see `Bugs.md` #1). |
| **Relevance to Project** | Resolves the ADR-003 comparison in principle without selecting either tool — and, together with topic 8, is why the execution layer ultimately went to neither (see ADR-004). |

---

## 6. Per-agent budget limits are not queryable — consequences for the routing model

| Field | Detail |
|---|---|
| **Topic** | The project routes work by "token budget of each coding agent." Whether that quantity actually exists and can be read at runtime. |
| **Date** | 2026-08-03 |
| **Findings** | None of the four intended agents exposes "tokens remaining" as a readable value, and their limits are not even the same *kind* of quantity. Claude Code is bounded by rolling usage windows on a subscription, or $/token on an API key; usage is reported per call but remaining headroom is not exposed. Codex is bounded by rate-limit windows on a ChatGPT plan, or $/token on an API key; also not queryable. Gemini CLI's free tier is bounded by **request count** (per-minute and per-day), not tokens at all. DeepSeek is pure $/token with only an account balance readable. Separately: DeepSeek is a *model*, not an agentic coding CLI in the class of Claude Code or Codex — using it as a first-class coding agent requires pairing it with a harness that accepts a custom OpenAI-compatible endpoint (Aider, OpenCode, or similar), otherwise it is limited to non-agentic roles (reviewer, planner, summarizer) served by a plain API call. |
| **Conclusion** | "Remaining tokens" cannot be modelled directly. The workable substitute is a normalized per-agent ledger with a `limit_type` (rolling window / daily requests / monetary / none), user-configured limits, consumption accumulated from each call's reported usage, and a state field (available / degraded / exhausted / cooldown). It must be updated from two sources with an explicit precedence: proactive accumulation is an *estimate* used for pre-emptive routing, while a non-zero exit plus a rate-limit message is the *authoritative* signal that triggers failover and a cooldown. Vendor error strings should live in configuration rather than code, since they get reworded. Two corollaries: reserve headroom before dispatching (a mid-task death costs both the partial work and a cold-start handoff), and price agent-switching into the routing decision, since returning to an agent with a live native session is far cheaper than cold-starting a different vendor. |
| **Relevance to Project** | Constrains the "Agent Router" in `Architecture.md`: routing is necessarily best-effort estimation with a reactive override, not budget arithmetic. Also determines DeepSeek's role, which remains unresolved pending a hands-on check of which harness it runs under. |

---

## 7. Workflow-engine layer — Langflow, LangGraph, Temporal, or hand-rolled

| Field | Detail |
|---|---|
| **Topic** | Which layer should own workflow state, routing, retries, cycles, and crash recovery — re-examined after a provisional stack table named Langflow while ADR-002 had chosen LangGraph |
| **Date** | 2026-08-03 |
| **Findings** | Langflow and LangGraph are not interchangeable. Langflow is a visual drag-and-drop builder over LangChain — good for prototyping and for showing a flow diagram to someone. LangGraph is a durable state-machine runtime with cycles, conditional edges, checkpointers, interrupts, and resume-from-checkpoint. What this project needs from that layer — fix→recheck cycles, conditional routing on agent state, checkpoint/resume after a crash, mid-run interruption, and nodes that block for minutes on a subprocess — is LangGraph's feature list, not Langflow's; and once every node is custom Python wrapping a subprocess, the visual surface stops paying for itself. Temporal was also reviewed as the durable-execution option in this slot (workflow durability, retries, timeouts, and recovery as first-class primitives, at the cost of running a server and adopting its worker/determinism model); the detailed evaluation notes for it were not captured in the session transcripts and should be recorded here when available. A third option was raised: for a first version the described workflow is a linear pipeline with a retry loop — roughly a couple hundred lines of plain Python over a persisted state document — and the genuinely hard parts (the agent adapter layer, the handoff packet, the budget ledger) get no help from any of these engines. |
| **Conclusion** | Langflow is not the right choice for the runtime; if a visual surface is wanted it belongs on top as an authoring/monitoring UI, not as the engine. Between LangGraph and Temporal, the choice can be deferred: adopting a workflow framework before the adapter/handoff/ledger layers exist risks encoding the wrong shape. This question was ultimately overtaken by the Maestro decision (topic 8, ADR-004), which supplies its own run/step lifecycle — but it remains live for whatever executes *inside* that lifecycle. |
| **Relevance to Project** | Directly amends ADR-002's LangGraph choice and removes Langflow from the stack table in `Architecture.md`. |

---

## 8. Maestro-flow (Ralph) and Flow-next as mid-task cross-agent delegation frameworks

| Field | Detail |
|---|---|
| **Topic** | Two Claude Code-native orchestration frameworks surfaced late in the search, evaluated for whether they perform mid-task switching between different coding-agent CLIs |
| **Date** | 2026-08-03 |
| **Findings** | **Maestro-flow / Ralph** defines three node types in a chain, and the distinction is the important part: `skill` nodes run synchronously as a command execution in the host CLI; `cli` nodes run via `maestro delegate` as a *background CLI delegate execution* — this is the agent-switching mechanism, and it is architectural rather than bolted on; `decision` nodes re-evaluate results and decide whether to continue or insert a fix loop. A chain is therefore not locked to a single agent: individual steps typed `cli` hand off to a different backend CLI while the session continues. Its default lifecycle is plan → execute → verify → ✓post-verify → business-test → ✓post-business-test → review → ✓post-review → test-gen + test, where ✓ marks a decision gate; `--quality quick` collapses this to verify → CLI-review, and that CLI-review stage is a `cli` node, i.e. an actual delegate call to another agent. Its five built-in decision gates are all post-execution (post-verify, post-review, post-test, post-milestone) — there is no built-in gate that reviews a *plan* before execution begins, so a plan-review checkpoint has to be assembled manually with `maestro delegate`. **Flow-next** exposes the same two checkpoints as named, first-class commands (`plan-review`, `impl-review`) with a configured backend from setup, and is broadly configurable: `flowctl config set review.backend codex|repoprompt|copilot|cursor` swaps the reviewer; `work.delegate=codex` (off by default, consent-gated) offloads implementation itself; per-task reviewer routing lets one epic route different tasks to different reviewers; review granularity is selectable per-task or per-epic; optional stages (interview, qa, spec-completion-review) can be switched on; and the same gates run under three autonomy levels — manual, `pilot` (one stage per tick), or a fully autonomous Ralph loop. It is first-class on Claude Code, Codex, and Factory Droid, so even the host CLI driving the orchestration can be swapped. |
| **Conclusion** | Both frameworks demonstrate that mid-task delegation to a *different* coding-agent CLI is a solved primitive — `maestro delegate` and `flowctl`-routed review backends do concretely what the earlier survey found missing from every candidate. Flow-next is the more configurable of the two out of the box, particularly for pre-execution plan review, which Ralph requires hand-assembly for. Maestro was nevertheless selected (ADR-004) on the strength of its delegate primitive, run/step lifecycle, and persistent knowledge system, and because it is already installed and configured in this environment. |
| **Relevance to Project** | Supplies the execution/session layer that ADR-002 and ADR-003 left unresolved, and retires the Claw-vs-OpenHands question. See ADR-004. |

---
