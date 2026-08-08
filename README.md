# Bhai-To-Bhai

An open-source orchestration controller that coordinates existing coding-agent CLIs to take a software task from requirements through implementation, merge, review, and final verification in an external Git repository.

## What it does

- Runs a six-stage LangGraph pipeline: requirements → planner → wave orchestrator → merger → reviewer → supervisor.
- Pauses for material clarification, persists explicit user answers, and resumes from SQLite checkpoints.
- Converts planner dependencies into deterministic execution waves and runs independent coding tasks concurrently in isolated Git worktrees.
- Merges successful task branches into a provisional integration branch, verifies conflict resolutions, and rewinds rejected work for same-session rework.
- Reviews every merged wave, then audits the completed result against the original requirements; bounded retries are never reported as completion.
- Supports direct Claude Code and Codex execution, Maestro delegation, and a deterministic scripted stub through one non-raising adapter contract.
- Stores plans, task contracts, events, learnings, reviews, logs, costs, and session references outside the target checkout.
- Treats agent reports as claims and Git/filesystem observations as evidence.

## Status

**Production workflow implemented; live end-to-end validation still pending.**

The real system is `orchestrator/`. It contains the complete six-stage controller, adapters, checkpointing, artifact persistence, worktree/branch management, routing, rollback, bounded feedback loops, and CLI entry/resume behavior. The implementation arrived in the `Workflow implemented` change with **8,888 insertions across 53 files**.

The orchestrator has **189 tests across 10 test files**, recorded passing on 2026-08-07. The suite uses the first-class stub transport, so graph, state, worktree, merge, rework, replan, and failure behavior can be exercised deterministically without paid agent calls.

One real Claude adapter probe is preserved in `orchestrator/run_logs/live_probe_20260807_194239.debug.log`: structured output parsed successfully, a session id was captured, and the one-turn call cost `$0.067745`. There has not yet been a paid six-agent end-to-end run against a real target repository.

### Repository tracks

| Track | Purpose | State |
|---|---|---|
| `orchestrator/` | Production six-stage workflow and its tests. | Implemented; 189-test stub-backed suite passes. |
| `yt_tutorial/` | Simplified LangGraph, Claude Code, Codex, and multi-agent experiments used to learn the failure modes that shaped production. | Learning sandbox only; not imported by production. |

Open production findings are recorded in `docs/Bugs.md` #26–#28: the merge agent receives incomplete “ours” context, requirements routing has a duplicate unwired implementation, and successful runs leave task worktrees on disk. The standalone preflight's bare Maestro lookup remains the open half of bug #3.

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

Inspect the effective topology and agent configuration without starting a run:

```bash
python orchestrator/main.py --goal "Add an OAuth2 refresh flow" --target ../MyApp --dry-run
```

Run against a different Git repository:

```bash
python orchestrator/main.py --goal "Add an OAuth2 refresh flow" --target ../MyApp
```

Resume a paused or interrupted run:

```bash
python orchestrator/main.py --resume <run-id>
```

Maestro is installed repo-locally under `node_modules/.bin/`; the Maestro adapter resolves that location before falling back to `PATH`.

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
