# Bhai-To-Bhai

An open-source orchestration controller that coordinates existing coding-agent CLIs to take a software task from requirements through implementation, merge, review, and final verification in an external Git repository.

## What it does

- Runs a six-stage LangGraph pipeline: requirements → planner → wave orchestrator → merger → reviewer → supervisor.
- Pauses for material clarification, persists explicit user answers, and resumes from SQLite checkpoints.
- Converts planner dependencies into deterministic execution waves and runs independent coding tasks concurrently in isolated Git worktrees, using a planner-sized coding-agent roster when supplied and falling back to two configurable coding-agent slots otherwise.
- Merges successful task branches into a provisional integration branch, verifies conflict resolutions, and rewinds rejected work for same-session rework.
- Reviews every merged wave, then audits the completed result against the original requirements; bounded retries are never reported as completion.
- Supports direct Claude Code and Codex execution, locally-hosted Ollama models (routed through the Codex harness), Maestro delegation, and a deterministic scripted stub through one non-raising adapter contract; Windows deadlines terminate the complete npm CLI process tree rather than only its wrapper.
- Stores plans, task contracts, events, learnings, reviews, logs, costs, and session references outside the target checkout; agents read run artifacts by path rather than receiving full pasted copies.
- Lets coding agents append findings directly to the run's shared `learnings.md` through an OS-locked helper, so parallel writers do not corrupt the file.
- Treats agent reports as claims and Git/filesystem observations as evidence.

## Status

**Production workflow implemented; live end-to-end validation still pending.**

The real system is `orchestrator/`. It contains the complete six-stage controller, adapters, checkpointing, artifact persistence, worktree/branch management, routing, rollback, bounded feedback loops, and CLI entry/resume behavior. The implementation arrived in the `Workflow implemented` change with **8,888 insertions across 53 files**.

The orchestrator has **209 tests across 10 test files**, recorded passing after the 2026-08-11 Ollama-adapter changes (197 on both 2026-08-09 after the process-tree deadline/dual coding-slot changes and 2026-08-10 after the planner-sized roster/path-reference work; 189 on 2026-08-07; 193 after the 2026-08-08 session-resume/environment fixes). The suite uses the first-class stub transport, so graph, state, worktree, merge, rework, replan, and failure behavior can be exercised deterministically without paid agent calls.

One real Claude adapter probe is preserved in `orchestrator/run_logs/live_probe_20260807_194239.debug.log`: structured output parsed successfully, a session id was captured, and the one-turn call cost `$0.067745`. Two full live runs against real target repositories were diagnosed on 2026-08-08 (`docs/Research.md` topic 24): one failed at worktree setup against an uncommitted target repository (`docs/Bugs.md` #32), the other completed and was accepted by the supervisor but shipped a latent HTML rendering defect the pipeline's checks did not cover (`docs/Bugs.md` #33). On 2026-08-09, a multi-task website/API stress run proved real parallel task execution and exposed two continuity gaps: reviewer rework resets all of a wave's integrated work, and coding worktrees then received artifact contents only as prompt snapshots rather than direct shared access (`docs/Research.md` topic 28). On 2026-08-10, `run-20260810-162135` validated planner-sized coding rosters and direct `learnings.md` appends in a paid smoke run, while later pricing-tool runs exposed a stale model alias and live quota exhaustion (`docs/Research.md` topic 31). On 2026-08-11, a Claude weekly rate limit forced a locally-hosted Ollama fallback (`orchestrator/adapters/ollama.py`, routed through the Codex harness only after a same-day Claude-harness routing bug was found and removed, `docs/Bugs.md` #39): it proved reliable for single-shot structured-output stages, but real coding-task dispatch on the strongest models available on the test hardware produced false completion reports and, once, a fabricated `learnings.md` entry — caught by the pipeline's evidence checks in every case, and attributed by the session's own conclusion to model capacity rather than to the orchestrator (`docs/Bugs.md` #40–#41, `docs/Research.md` topics 32–33). There has not yet been a paid six-agent end-to-end run that is both live and defect-free.

### Repository tracks

| Track | Purpose | State |
|---|---|---|
| `orchestrator/` | Production six-stage workflow and its tests. | Implemented; 209-test stub-backed suite passes. |
| `yt_tutorial/` | Simplified LangGraph, Claude Code, Codex, and multi-agent experiments used to learn the failure modes that shaped production. | Learning sandbox only; not imported by production. |

Open production findings are recorded in `docs/Bugs.md` #26–#28 (incomplete merge context, duplicate requirements routing, and successful-run worktree leakage), #32–#33 (unborn-repository setup and an accepted HTML rendering defect), #35–#36 (wave-wide rollback removes completed integrations, and run artifacts are still not project-scoped across runs), #38 (valid roster choices do not yet account for live quota exhaustion), #40 (a local Ollama model breaking the JSON-only output contract), and #41 (local coding-agent models reporting false completion and, once, fabricating a shared `learnings.md` entry — a model-capability limit rather than a pipeline defect). Bug #34—the Windows npm-shim timeout that left `node.exe` alive and hung forever after a deadline—is fixed across all three vendor adapters. Bug #37—a stale `sonnet-5` model alias in the roster menu—was fixed by switching the menu to live aliases. Bug #39—a short-lived option that routed Ollama dispatches through Claude Code's harness instead of Codex's—was removed the same day it failed in production. The selective-rework and project-scoped-context changes are planned only; they are not implemented yet.

## Pipeline

```text
START → requirements → plan → orchestrate → merge → review → supervise → END
              │                    ↑          │             │
              └── clarify ─────────┘          └── rework ───┘
                                    next wave ──────────────┘
                           plan ←──────── replan
```

`Orchestrate → Merge → Review` repeats for each dependency wave. Reviewer feedback returns to the same coding sessions where supported. Supervisor feedback resets the integration branch to the run base and returns to planning. Disabling reviewer or supervisor removes that node from the compiled graph.

## Run it

Install dependencies into a Python environment:

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
npm install
```

`requirements.txt` includes `python-dotenv`; without it, importing `orchestrator/config.py` fails even before a run starts.

### Before you run it

The orchestrator does not currently preflight these on its own — `orchestrator/preflight.py` checks them but is not called automatically by `main.py`, so run it by hand (`python orchestrator/preflight.py`) before a real target:

- **The `--target` path must already exist and be a Git repository with at least one commit on its default branch.** A freshly `git init`-ed repository with no commits passes the "is a repo" check but fails later, mid-run, at worktree setup — `fatal: Needed a single revision` / `fatal: not a valid object name: 'master'` — only after requirements and planning have already run and spent agent budget (`docs/Bugs.md` #32). `git init && git commit --allow-empty -m "init"` is enough to satisfy it.
- **Whichever agent CLI is selected in `config.AGENTS` (Claude Code and/or Codex) must be installed and resolvable from the environment the orchestrator's subprocesses run in**, not just from an interactive shell where `PATH` was fixed by hand. On Windows this means the `.cmd` shim specifically — `codex.cmd`, `claude.cmd`/`claude.exe` — since a name that resolves in one shell does not automatically resolve the same way inside a spawned subprocess.
- **Maestro resolves repo-locally, not globally**: `node_modules/.bin/maestro.cmd` on Windows (`.bin/maestro` elsewhere) takes precedence over `PATH`, configurable via the `MAESTRO_BIN` env var. Run `npm install` first — without it, both the standalone preflight and any agent's own `maestro search` tool calls fail with `maestro: command not found`.
- **MongoDB should be reachable** (`pymongo` is pinned in `requirements.txt` specifically for the preflight's connectivity check); the pipeline itself does not depend on it for state, which lives in SQLite checkpoints and the run's artifact directory instead.

Inspect the effective topology and agent configuration without starting a run:

```bash
python orchestrator/main.py --goal "Add an OAuth2 refresh flow" --target ../MyApp --dry-run
```

The current default roster is mixed: Claude Haiku handles requirements, wave orchestration, and merge; Codex handles planning; Claude Sonnet handles review and supervision. The planner can request a run-specific coding-agent roster from the configured small/medium and expert menus, capped by `MAX_CODING_AGENT_COUNT`; coding tasks round-robin across that roster. If no valid roster is supplied, tasks fall back to slot A (Codex CLI default) and slot B (Claude Sonnet). Override the fallback slots independently with `CODING_AGENT_A_BACKEND` / `CODING_AGENT_A_MODEL` and `CODING_AGENT_B_BACKEND` / `CODING_AGENT_B_MODEL`. The legacy shared `CODING_AGENT_BACKEND` / `CODING_AGENT_MODEL` variables remain valid fallbacks, so both slots can still be configured identically.

Run against a different Git repository:

```bash
python orchestrator/main.py --goal "Add an OAuth2 refresh flow" --target ../MyApp
```

Resume a paused or interrupted run:

```bash
python orchestrator/main.py --resume <run-id>
```

## Documentation

| File | Purpose |
|---|---|
| [`docs/Status.md`](docs/Status.md) | Chronological project log |
| [`docs/Architecture.md`](docs/Architecture.md) | Implemented runtime, modules, data flow, persistence, and evidence model |
| [`docs/Decisions.md`](docs/Decisions.md) | Numbered ADRs explaining why the design was chosen |
| [`docs/Research.md`](docs/Research.md) | Findings from tools, experiments, ChatGPT/Claude research, and prior transcripts |
| [`docs/Bugs.md`](docs/Bugs.md) | Environment, sandbox, adapter, and production-code defects |
| [`Build-Guide.md`](Build-Guide.md) | Original staged build plan and v0 failover acceptance target |

Start with `docs/Architecture.md` for the system, `docs/Status.md` for the history, and `docs/Bugs.md` for known limitations.
