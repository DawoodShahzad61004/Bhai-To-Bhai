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
