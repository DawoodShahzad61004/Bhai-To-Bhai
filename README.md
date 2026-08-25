# Bhai-To-Bhai

An open-source orchestration controller that coordinates existing coding-agent CLIs to take a software task from requirements through implementation, merge, review, and final verification in an external Git repository.

## What it does

- Runs a six-stage LangGraph pipeline: requirements → planner → wave orchestrator → merger → reviewer → supervisor.
- Pauses for material clarification, persists explicit user answers, and resumes from SQLite checkpoints.
- Converts planner dependencies into deterministic execution waves and runs independent coding tasks concurrently in isolated Git worktrees, using a planner-sized coding-agent roster when supplied and falling back to two configurable coding-agent slots otherwise.
- Merges successful task branches into a provisional integration branch, verifies conflict resolutions, and on rework rebuilds integration from kept task results while redispatching only the rejected tasks for same-session rework.
- Reviews every merged wave with a per-task keep/rework verdict, then audits the completed result against the original requirements; bounded retries are never reported as completion.
- Supports direct Claude Code, Codex, GitHub Copilot, and Gemini CLI execution, locally-hosted Ollama models (routed through the Codex harness), configurable OpenAI-compatible local servers through the separate `direct:local_llm` bridge, optional Maestro delegation, and a deterministic scripted stub through one non-raising adapter contract; Windows deadlines terminate the complete npm CLI process tree rather than only its wrapper.
- Stores project memory in the target repository's `runs/` directory (`context.md`, locked `learnings.md`, and `user_choices.md`) while keeping plans, task contracts, events, and reviews under run-specific paths; agents read artifacts by path rather than receiving full pasted copies.
- Lets coding agents append findings directly to the target project's shared `learnings.md` through an OS-locked helper, so parallel writers do not corrupt the cross-run evidence file.
- Treats agent reports as claims and Git/filesystem observations as evidence.

## Status

**Production workflow implemented; live end-to-end validation still pending.**

The real system is `orchestrator/`. It contains the complete six-stage controller, adapters, checkpointing, artifact persistence, worktree/branch management, routing, rollback, bounded feedback loops, and CLI entry/resume behavior. The implementation arrived in the `Workflow implemented` change with **8,888 insertions across 53 files**.

The suite now **collects 245 tests across 12 test files: 244 passing plus one pre-existing, unrelated failure** (`test_copilot_adapter_builds_noninteractive_command`, confirmed pre-existing by stashing the Aug 24 changes and rerunning it in isolation). The last documented full green run in repository history remains the Aug 18 local-LLM validation session at **231 passing tests**, run through the repository virtual environment with an isolated pytest temp directory. The Aug 24 follow-up added coverage for task-level reviewer rework: mixed keep/rework outcomes, a failed task forced to rework in code, and reviewer prompts excluding already-kept tasks; every existing wave-revert test passed unmodified. The suite uses the first-class stub transport, so graph, state, worktree, merge, rework, replan, and failure behavior can be exercised deterministically without paid agent calls.

One real Claude adapter probe is preserved in `orchestrator/run_logs/live_probe_20260807_194239.debug.log`: structured output parsed successfully, a session id was captured, and the one-turn call cost `$0.067745`. Two full live runs against real target repositories were diagnosed on 2026-08-08 (`docs/Research.md` topic 24): one failed at worktree setup against an uncommitted target repository (`docs/Bugs.md` #32), the other completed and was accepted by the supervisor but shipped a latent HTML rendering defect the pipeline's checks did not cover (`docs/Bugs.md` #33). On 2026-08-09, a multi-task website/API stress run proved real parallel task execution and exposed two continuity gaps: reviewer rework reset all of a wave's integrated work, and coding worktrees then received artifact contents only as prompt snapshots rather than direct shared access (`docs/Research.md` topic 28). The wave-wide reset gap was closed on 2026-08-24: reviewer rework is now task-level, rebuilding integration from kept task results and redispatching only rejected tasks (`docs/Bugs.md` #35, `docs/Decisions.md` ADR-035). On 2026-08-10, `run-20260810-162135` validated planner-sized coding rosters and direct `learnings.md` appends in a paid smoke run, while later pricing-tool runs exposed a stale model alias and live quota exhaustion (`docs/Research.md` topic 31). On 2026-08-11, a Claude weekly rate limit forced a locally-hosted Ollama fallback (`orchestrator/adapters/ollama.py`, routed through the Codex harness only after a same-day Claude-harness routing bug was found and removed, `docs/Bugs.md` #39): it proved reliable for single-shot structured-output stages, but real coding-task dispatch on the strongest models available on the test hardware produced false completion reports and, once, a fabricated `learnings.md` entry — caught by the pipeline's evidence checks in every case, and attributed by the session's own conclusion to model capacity rather than to the orchestrator (`docs/Bugs.md` #40-#41, `docs/Research.md` topics 32-33). The Aug 18-20 follow-up then added the direct local-server Codex-harness bridge, Copilot's same-session structured-output repair, Gemini as a direct adapter, and Local LLM memory/harness isolation (`docs/Bugs.md` #47-#50; `docs/Research.md` topics 39-44). There has not yet been a paid six-agent end-to-end run that is both live and defect-free.

### Repository tracks

| Track | Purpose | State |
|---|---|---|
| `orchestrator/` | Production six-stage workflow and its tests. | Implemented; 231 tests passing. |
| `yt_tutorial/` | Simplified LangGraph, Claude Code, Codex, and multi-agent experiments used to learn the failure modes that shaped production. | Learning sandbox only; not imported by production. |

Open production findings are recorded in `docs/Bugs.md` #26-#28, #32-#33, #38, #40-#43, #50, and #51. Bug #35 (wave-wide rework reset) and #36's project-scope artifact gap are fixed: reviewer rework is now task-level, and shared memory lives under the target repository while audit records remain per-run. Bug #42 still depends on Copilot/GitHub service health, #43 tracks subscription-gated Ollama Cloud roster entries, and #44 is now closed by the shipped `direct:local_llm` backend. Bug #34 - the Windows npm-shim timeout - remains fixed across all three vendor adapters; #37, #39, #46, and #47 were fixed by correcting the model menu, removing the incompatible Claude Ollama harness path, matching the live Copilot event shape, and adding a same-session JSON repair path. The Aug 20 Local LLM follow-up also closed #48-#49. Bug #51, found 2026-08-24, is a new open finding: a blind `git add -A` can commit a build artifact that collides with one already sitting untracked in the shared integration checkout, aborting the merge.

## Pipeline

```text
START → requirements → plan → orchestrate → merge → review → supervise → END
              │                    ↑          │             │
              └── clarify ─────────┘          └── rework ───┘
                                    next wave ──────────────┘
                           plan ←──────── replan
```

`Orchestrate → Merge → Review` repeats for each dependency wave. Reviewer feedback is per-task: rejected tasks return to the same coding sessions where supported, while kept tasks' merged work survives into the rebuilt integration branch untouched. Supervisor feedback resets the integration branch to the run base and returns to planning. Disabling reviewer or supervisor removes that node from the compiled graph.

## Run it

Install dependencies into a Python environment:

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt
```

`requirements.txt` includes `python-dotenv`; without it, importing `orchestrator/config.py` fails even before a run starts.

### Before you run it

The orchestrator does not currently preflight these on its own — `orchestrator/preflight.py` checks them but is not called automatically by `main.py`, so run it by hand (`python orchestrator/preflight.py`) before a real target:

- **The `--target` path must already exist and be a Git repository with at least one commit on its default branch.** A freshly `git init`-ed repository with no commits passes the "is a repo" check but fails later, mid-run, at worktree setup — `fatal: Needed a single revision` / `fatal: not a valid object name: 'master'` — only after requirements and planning have already run and spent agent budget (`docs/Bugs.md` #32). `git init && git commit --allow-empty -m "init"` is enough to satisfy it.
- **Whichever agent CLI is selected in `config.AGENTS` (currently Claude, Gemini, and Codex by default) must be installed and resolvable from the environment the orchestrator's subprocesses run in**, not just from an interactive shell where `PATH` was fixed by hand. On Windows this means the actual shim/binary the subprocess will launch — for example `codex.cmd`, `claude.cmd`/`claude.exe`, or `gemini.cmd` — since a name that resolves in one shell does not automatically resolve the same way inside a spawned subprocess.
- **If you use `backend=\"gemini\"`, set `GEMINI_API_KEY` in the controller `.env` or parent process environment.** The Gemini adapter reads credentials from the controller side, not from the target repository, and current defaults use Gemini for requirements, wave orchestration, and merge.
- **If you opt into `INVOCATION=maestro`, provide a Maestro binary explicitly**: the adapter still exists, but this repository no longer bundles `maestro-flow` in `package.json`. Set `MAESTRO_BIN` or ensure `maestro` is on `PATH`; otherwise the optional transport will fail at preflight/dispatch time.
- **MongoDB should be reachable** (`pymongo` is pinned in `requirements.txt` specifically for the preflight's connectivity check); the pipeline itself does not depend on it for state, which lives in SQLite checkpoints and the run's artifact directory instead.

Inspect the effective topology and agent configuration without starting a run:

```bash
python orchestrator/main.py --goal "Add an OAuth2 refresh flow" --target ../MyApp --dry-run
```

The current checked-in roster reflects the Aug 24 rebalance back toward Claude/Gemini: Claude Code Haiku handles requirements, Gemini CLI `gemini-3.1-flash-lite` handles wave orchestration and merge, Claude Code Sonnet handles planning, and Codex CLI default handles review and supervision; coding tasks still fall back to Codex in slots A and B unless the planner chooses a different run-specific roster from the configured menus. The planner can request a run-specific coding-agent roster from the configured small/medium and expert menus, capped by `MAX_CODING_AGENT_COUNT`; in the current checkout both menus expose the Local LLM Qwen server entry rather than Gemini or Copilot. Coding tasks round-robin across that roster. If no valid roster is supplied, tasks fall back to the configurable slots A and B. Override them independently with `CODING_AGENT_A_BACKEND` / `CODING_AGENT_A_MODEL` and `CODING_AGENT_B_BACKEND` / `CODING_AGENT_B_MODEL`. The legacy shared `CODING_AGENT_BACKEND` / `CODING_AGENT_MODEL` variables remain valid fallbacks, so both slots can still be configured identically.

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
