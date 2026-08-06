# Bhai-To-Bhai

An open-source, locally deployable orchestration platform that coordinates multiple *existing* coding agents (Claude Code, Codex, Gemini CLI, OpenCode/Aider, and other adapters) to complete software-engineering tasks end-to-end — from initial planning through final verification — instead of implementing its own coding agent.

## What it does

- Dynamically selects the most suitable agent for each stage of a task based on task type, repository context, required capabilities, prior performance, availability, cost, and configurable routing policies.
- Switches between agents *during* a single task, following structured workflows such as planner → implementer → reviewer → fixer.
- Maintains one canonical task context (objective, requirements, constraints, acceptance criteria, decisions, completed/remaining work, failed attempts) across every agent handoff.
- Tracks each agent's native resumable session reference so a previously used agent can pick up where it left off when control returns to it.
- Runs deterministic verification (tests, linting, type checks, security scans, project-specific acceptance checks) instead of trusting an agent's own claim of completion.
- Routes failed checks and review findings to the most appropriate agent for correction, then re-verifies and independently reviews before accepting changes.
- Persists workflow state, logs, traces, costs, and audit history so long-running tasks are observable, resumable, and recoverable after interruption — including optional human approval gates.
- Deploys locally or in containers, with an adapter model for adding new coding-agent CLIs, and never claims to transfer hidden model state between vendors — only explicit context and session references.

## Status

**Early implementation — Stage 0 of 8.** The tool search that ran through July is closed. A 35-item requirements checklist was used to evaluate ten orchestration frameworks plus Command Code and OpenHands Agent Canvas, and none fully satisfied it; the recurring gap was that "multi-CLI execution runtime" and "dynamic cross-agent routing" were always separate concerns, with no candidate merging both.

That gap closed on 2026-08-03. **Maestro** was adopted as the external-agent execution and delegation layer (ADR-004) after hands-on evaluation ruled out Claw Orchestrator and OpenHands, and after Maestro-flow and Flow-next demonstrated that mid-task delegation to a *different* coding-agent CLI is already a solved primitive. Alongside it, the two mechanics the failover feature actually rests on were specified: handoff packets are reconstructed on agent entry from durable artifacts rather than summarized on exit (ADR-005), and agent budgets are modelled as a configured ledger with reactive limit detection as the authoritative signal, since no vendor exposes remaining allowance (ADR-006).

Current stack: Maestro (agent execution/delegation, installed repo-locally) · MongoDB (canonical task context, runs, events, audit) · Git worktrees (isolation) · exit-code-based check runner (verification) · Phoenix/OpenTelemetry (tracing). The workflow-engine slot — LangGraph, Temporal, or hand-rolled — is deliberately deferred, since Maestro supplies its own run/step lifecycle.

### What actually exists

Two deliberately separate tracks, sharing no code:

| Track | Contents | State |
|---|---|---|
| `orchestrator/` | The real system. `preflight.py` — resolves agent binaries, pings MongoDB, checks `git worktree`. | Build Guide **Stage 0**. Currently fails two of its own checks ([`docs/Bugs.md`](docs/Bugs.md) #3). |
| `yt_tutorial/` | A disposable LangGraph hierarchical-supervisor sandbox (ADR-008), built to learn the multi-agent primitives hands-on before Stage 1. Since 2026-08-06 it also holds `claude_agents/` — a second implementation of every supervisor and worker on the Claude Code CLI (ADR-014) — and `temp_agents/`, a two-agent demo running Claude Code and Codex concurrently. | **Runs end to end** on either implementation, selected by a config flag. It now contains the only code in this repository that invokes an agent CLI at all. |

None of the architecture's components — Agent Router, Canonical Task Context Store, Handoff Builder, Check Runner, Event Log — is implemented yet. Next up is Stage 1, the durable artifact layer, which ADR-005 identifies as load-bearing and the first thing to build.

### What the sandbox returned

Running it, rather than reading about it, produced nineteen defects (`Bugs.md` #7–#25), seven decisions that carry into the orchestrator (ADR-010 through ADR-016), and seven research topics (12–18). The most useful result is a failure that looks exactly like a success: a complete pipeline run that exited 0, raised nothing, decided `FINISH` in five steps, and **produced no document** — the top-level supervisor never routed to the team that would have written the file, and the guard added to bound an earlier infinite loop quietly ended the task instead. Every completion signal the system emits about itself agreed with the others, and all of them were wrong; only comparing the produced artefacts against the original request revealed it. That is the concrete case behind two of this project's standing commitments — completion is decided by deterministic exit-code checks and never by an agent's self-report, and any bound placed on an agent's work must record whether it stopped *because done* or *because bounded*.

That failure has since been half closed, and the half that closed is the half that was a design defect: a delegated team now receives the whole conversation rather than the last message, so it no longer re-researches its own previous report and the writing team finally sees the file name it is meant to produce. The other half is routing judgement, which is not fixable in code — and separating those two was itself the reason for giving every node a second implementation on the Claude Code CLI. Handed the identical graph, prompts, and request, it routed correctly and produced the document. Same topology, different agent.

Four further findings transfer directly. "OpenAI-compatible" is a claim about requests, not a contract about responses, so vendor quirks must be normalized inside an adapter at the boundary and per-feature capability established by probe (ADR-010). Termination is structural rather than promptable, and more generally **a rule loses to a fact** — a system-prompt prohibition lost to a file name in the user's request, and a role brief's "the workspace directory" lost to the CLI's own scratchpad path — so constraints belong at the tool boundary, where a tool's *return value* turns out to be the channel a small model actually heeds (ADR-011). A limit must name its unit: a per-read socket timeout does not bound a server that streams while making no progress, which is the same blind spot `maestro delegate --timeout` has (ADR-016). And an adapter must never raise past the router, because a supervisor can route around a worker that failed but not around a traceback.

### What driving a real agent CLI cost

The newest and most directly applicable material, since Build Guide Stage 4 is an adapter layer and this is the first time the repository has invoked an external coding agent. `Research.md` topic 17 catalogues nine behaviours a coding-agent CLI has that a model API does not, every one found by a run failing: it arrives with its own repository conventions and acts on them; its own system prompt competes with yours and wins on specificity; its tools attach asynchronously so the first turn may be planned without them; its child processes' diagnostics are captured rather than forwarded; it prompts for permission with no terminal to answer; multi-line prompts passed as arguments are truncated by the platform's shell; the real error is at the end of stderr, not the head; the model name is an account entitlement rather than a parameter; and the final answer needs a designated channel. Two of these produced an agent reporting work it had not done, which is the same signature as the clean-run-that-produced-nothing.

One thing got easier. Claude Code reports `total_cost_usd`, per-model token counts, and a `session_id` on **every** invocation — the first readable consumption in this project, where the local endpoint reports none and Codex reports none. A measured five-step run cost $1.05 across 9 calls. It is consumption rather than remaining, so it narrows the budget ledger's estimate without replacing the reactive failover signal; and because it is per-vendor, a two-agent v0 has one agent fed by measurement and one by configured estimate — exactly the mixed state ADR-006's "degraded" ledger field was specified for, now with an instance instead of a hypothetical.

The v0 target, restated because it is the clearest definition of done: give the system a real task in a real repo, set one agent's budget artificially low so it will be exhausted partway through, and have it plan, start implementing, detect the limit, fail over to a second agent mid-task, continue from the existing partial work rather than restarting, pass the deterministic check runner, and leave a complete audit trail. [`Build-Guide.md`](Build-Guide.md) breaks that into eight testable assertions.

## Documentation

This project tracks its own history and reasoning in `docs/`, using the 5-file system described in `Markdown-Tracking-Guide.md`:

| File | Purpose |
|---|---|
| [`docs/Status.md`](docs/Status.md) | Chronological log of what happened, session by session |
| [`docs/Architecture.md`](docs/Architecture.md) | Target architecture, what is actually built against it, and how it got there |
| [`docs/Decisions.md`](docs/Decisions.md) | Numbered ADRs — why X was chosen over Y |
| [`docs/Research.md`](docs/Research.md) | What was learned from investigating tools, papers, and prior transcripts |
| [`docs/Bugs.md`](docs/Bugs.md) | Bug records — environment failures from the evaluation phase, plus application defects since implementation began |
| [`Build-Guide.md`](Build-Guide.md) | Phased build order (Stages 0–8) and the v0 acceptance test |

Start with `docs/Status.md` for the latest session, `docs/Architecture.md` for the big picture, or `Build-Guide.md` for what to build next.

## Setup notes

Maestro is a **repo-local** npm dependency, not a global install (ADR-007), so it is not on `PATH`:

```bash
npm install                          # installs maestro-flow 0.5.61 into node_modules/
./node_modules/.bin/maestro --version
```

Claude Code hooks for this project live in `.claude/settings.json`, which is `.gitignore`d — a fresh clone will not have them and will need them recreated. `~/.maestro/` (config, workflows, templates) is intentionally global and shared.
