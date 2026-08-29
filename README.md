# Bhai-To-Bhai

An open-source orchestration controller that coordinates existing coding-agent CLIs to take a software task from requirements through implementation, merge, review, and final verification in an external Git repository.

## What it does

- Runs a six-stage LangGraph pipeline: requirements → planner → wave orchestrator → merger → reviewer → supervisor.
- Pauses for material clarification, persists explicit user answers, and resumes from SQLite checkpoints.
- Converts planner dependencies into deterministic execution waves and runs independent coding tasks concurrently in isolated Git worktrees, using a planner-sized coding-agent roster when supplied and falling back to two configurable coding-agent slots otherwise.
- Merges successful task branches into a provisional integration branch, verifies conflict resolutions, and on rework rebuilds integration from kept task results while redispatching only the rejected tasks for same-session rework.
- Reviews every merged wave with a per-task keep/rework verdict, then audits the completed result against the original requirements; bounded retries are never reported as completion.
- Supports direct Claude Code, Codex, GitHub Copilot, and Gemini CLI execution, locally-hosted Ollama models (routed through the Codex harness), configurable OpenAI-compatible local servers through the separate `direct:local_llm` bridge, optional Maestro delegation, and a deterministic scripted stub through one non-raising adapter contract; Windows deadlines terminate the complete npm CLI process tree rather than only its wrapper.
- Implements a transport-agnostic continuation-nudge loop for coding agents: when a small/local model sends narration-only prose instead of the required JSON status object, resumes the same session with a nudge to complete the reply (ADR-036), bounded by a configurable attempt limit to prevent loops; the mechanism works identically across all CLI backends.
- Stores project memory in the target repository's `runs/` directory (`context.md`, locked `learnings.md`, and `user_choices.md`) while keeping plans, task contracts, events, and reviews under run-specific paths; agents read artifacts by path rather than receiving full pasted copies.
- Lets coding agents append findings directly to the target project's shared `learnings.md` through an OS-locked helper, so parallel writers do not corrupt the cross-run evidence file; install/build/test commands run through a `run-shared` wrapper that auto-broadcasts a live peer's finding into a failing command's own output, so same-wave agents hitting the same problem seconds apart benefit from each other in real time (`docs/Decisions.md` ADR-042).
- Treats agent reports as claims and Git/filesystem observations as evidence.

## Status

**Production workflow implemented; live end-to-end validation still pending.**

The real system is `orchestrator/`. It contains the complete six-stage controller, adapters, checkpointing, artifact persistence, worktree/branch management, routing, rollback, bounded feedback loops, and CLI entry/resume behavior. The implementation arrived in the `Workflow implemented` change with **8,888 insertions across 53 files**.

The suite now **collects 281 tests: 280 passing plus one pre-existing, unrelated failure** (`test_copilot_adapter_builds_noninteractive_command`, confirmed pre-existing by stashing changes and rerunning in isolation). The Aug 24 follow-up added coverage for task-level reviewer rework: mixed keep/rework outcomes, a failed task forced to rework in code, and reviewer prompts excluding already-kept tasks; every existing wave-revert test passed unmodified. The Aug 25 follow-up implemented the continuation-nudge loop for coding agents that emit prose instead of required JSON (Bugs #21, #23), verified across all backends without regression. The Aug 27 follow-up bundled seven reliability improvements: Copilot adapter contract detection (`reply with JSON` marker), un-doubled braces in frame prompts, PowerShell tool aliases, evidence file capping (reduced one prompt from 493K to target size), build-artifact exclusion via pathspec, block-reason propagation, and session rework cold-start. The Aug 28 follow-up split the model rosters into three tiers (SMALL_MODELS for read-only analysis tasks, MEDIUM_MODELS for bounded coding work, EXPERT for any-task capability), empirically reproduced and documented Ollama file-I/O limitations as a transport-layer constraint (Codex bridge lacks apply_patch, auto-rejects shell), and hardened small-model isolation (Ollama 18-feature disable set reduced token counts 65K-98K → 24K-41K). The same day, a second follow-up centralized `CODEX_SANDBOX`/`CODEX_APPROVAL_POLICY` into named config constants shared by every Codex-harness backend and moved their defaults to `danger-full-access`/`untrusted` — fixing, for every backend, the half of Bug #53 where non-interactive `codex exec` auto-rejected shell escalation with no human to approve it (`docs/Decisions.md` ADR-041) — then corrected the same day's tier split a second time three hours later. The suite uses the first-class stub transport, so graph, state, worktree, merge, rework, replan, and failure behavior can be exercised deterministically without paid agent calls.

One real Claude adapter probe is preserved in `orchestrator/run_logs/live_probe_20260807_194239.debug.log`: structured output parsed successfully, a session id was captured, and the one-turn call cost `$0.067745`. Two full live runs against real target repositories were diagnosed on 2026-08-08 (`docs/Research.md` topic 24): one failed at worktree setup against an uncommitted target repository (`docs/Bugs.md` #32), the other completed and was accepted by the supervisor but shipped a latent HTML rendering defect the pipeline's checks did not cover (`docs/Bugs.md` #33). On 2026-08-09, a multi-task website/API stress run proved real parallel task execution and exposed two continuity gaps: reviewer rework reset all of a wave's integrated work, and coding worktrees then received artifact contents only as prompt snapshots rather than direct shared access (`docs/Research.md` topic 28). The wave-wide reset gap was closed on 2026-08-24: reviewer rework is now task-level, rebuilding integration from kept task results and redispatching only rejected tasks (`docs/Bugs.md` #35, `docs/Decisions.md` ADR-035). On 2026-08-10, `run-20260810-162135` validated planner-sized coding rosters and direct `learnings.md` appends in a paid smoke run, while later pricing-tool runs exposed a stale model alias and live quota exhaustion (`docs/Research.md` topic 31). On 2026-08-11, a Claude weekly rate limit forced a locally-hosted Ollama fallback (`orchestrator/adapters/ollama.py`, routed through the Codex harness only after a same-day Claude-harness routing bug was found and removed, `docs/Bugs.md` #39): it proved reliable for single-shot structured-output stages, but real coding-task dispatch on the strongest models available on the test hardware produced false completion reports and, once, a fabricated `learnings.md` entry — caught by the pipeline's evidence checks in every case, and attributed by the session's own conclusion to model capacity rather than to the orchestrator (`docs/Bugs.md` #40-#41, `docs/Research.md` topics 32-33). The Aug 18-20 follow-up then added the direct local-server Codex-harness bridge, Copilot's same-session structured-output repair, Gemini as a direct adapter, and Local LLM memory/harness isolation (`docs/Bugs.md` #47-#50; `docs/Research.md` topics 39-44). On Aug 27-28, the project split model rosters into three tiers and established Ollama file-I/O as a known architectural constraint rather than a fixable bug, closing Bug #51 (artifact collision) and opening #52 (unbounded learnings.md growth) and #53 (Ollama file-I/O limitation). Later the same day, `run-20260828-175006` deliberately pinned an incompatible dependency (`react-transition-group@1.2.1` on React 18) to test parallel-agent behavior under the new sandbox/approval defaults: both coding agents ran shell commands with zero `blocked by policy` rejections, and the reviewer approved both tasks individually while the supervisor's own full Vitest run caught a pre-existing regression neither task's files touched and sent the run back for a replan — a clean production instance of the reviewer/supervisor evidence split (`docs/Research.md` topics 53-54). The same run also showed that "shared learnings" only helped across time, not across simultaneity: both agents independently hit the identical peer-dependency error roughly ten seconds apart and each diagnosed it alone, because `learnings.md` was read once at the start of a turn and once before finishing — neither point fell inside the window where a live peer's discovery could actually help, even though the OS-level write lock itself worked correctly (`docs/Bugs.md` #54, `docs/Research.md` topic 55). This was fixed on 2026-08-29: a new `run-shared` wrapper broadcasts a live peer's finding into a failing command's own output automatically, verified in a second production run reproducing the original bug's own conditions (`docs/Decisions.md` ADR-042, `docs/Research.md` topic 57). There has not yet been a paid six-agent end-to-end run that is both live and defect-free.

### Repository tracks

| Track | Purpose | State |
|---|---|---|
| `orchestrator/` | Production six-stage workflow and its tests. | Implemented; 280 tests passing (1 pre-existing timeout). |
| `yt_tutorial/` | Simplified LangGraph, Claude Code, Codex, and multi-agent experiments used to learn the failure modes that shaped production. | Learning sandbox only; not imported by production. |

Open production findings are recorded in `docs/Bugs.md` #26-#28, #32-#33, #38, #40-#43, #50, #52, and #53. Bug #35 (wave-wide rework reset), #36 (project-scope artifact gap), and #51 (build-artifact collision) are fixed: reviewer rework is now task-level; shared memory (context.md, learnings.md, user_choices.md) lives under `.bhai-artifacts/` outside target repositories while audit records remain per-run; and pathspec exclusions + merge-time cleanup prevent build artifacts from being staged. Bug #42 still depends on Copilot/GitHub service health, #43 tracks subscription-gated Ollama Cloud roster entries, and #44 is now closed by the shipped `direct:local_llm` backend. Bug #34 (Windows npm-shim timeout) remains fixed across all three vendor adapters; #37, #39, #46, and #47 were fixed by correcting the model menu, removing the incompatible Claude Ollama harness path, matching the live Copilot event shape, and adding a same-session JSON repair path. The Aug 20 Local LLM follow-up also closed #48-#49. Bug #52 (unbounded learnings.md growth across runs) and #53 (Ollama file-I/O constraint: Codex bridge lacks apply_patch, auto-rejects shell commands at the sandbox level) are new findings from the Aug 27-28 work; #53 is documented as an architectural constraint rather than a fixable bug and drives the three-tier model roster split (SMALL_MODELS read-only, MEDIUM_MODELS bounded coding only, EXPERT any-task). Bug #53 is now **partially fixed**: the same-day sandbox/approval centralization (`docs/Decisions.md` ADR-041) fixes the shell-fallback-auto-rejected half of its root cause for every backend, but the apply_patch-unavailable-for-Ollama half remains open and unverified under the new permissive defaults. Bug #54 is **fixed** (2026-08-29): a new `run-shared` wrapper broadcasts a live peer's finding into a failing command's own output automatically, closing the design gap where same-wave agents hitting an identical problem within seconds of each other could not previously benefit from one another (`docs/Decisions.md` ADR-042). Shipping that fix the same day surfaced and fixed three further findings — #55 (the fix's own peer-read cursor bug), #56 (a Copilot argv-length regression from the prompt growth the fix needed), and #57 (an encoding crash in the fix's new CLI process) — and a same-day follow-up (`docs/Decisions.md` ADR-043) partially mitigated #52 by deduplicating the fix's own repeat-failure auto-entries; #52's broader scope remains open and deliberately deferred.

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

The current checked-in roster reflects the Aug 28 afternoon rebalance: Claude Code Haiku handles requirements, Gemini CLI `gemini-3.1-flash-lite` handles wave orchestration and merge, and Codex CLI default handles planning, review, and supervision (planner moved from Claude Code Sonnet back to Codex the same day); coding tasks still fall back to Codex in slots A and B unless the planner chooses a different run-specific roster from the configured menus. The Aug 28 morning follow-up split the coding-agent rosters into three tiers: SMALL_MODELS (4B-class models for read-only/advisory work, empirically unsafe for file I/O), MEDIUM_MODELS (Copilot/local_llm/larger Ollama models for bounded real coding), and EXPERT (any-task capability); a same-day evening follow-up corrected which entries actually sit in each tier (`docs/Decisions.md` ADR-039 status update). Ollama backend is explicitly excluded from file-creating tasks due to a transport-layer constraint: the Codex→Ollama bridge lacks apply_patch and (as of the same afternoon's sandbox/approval centralization) no longer auto-rejects shell commands at the sandbox level for *other* backends, though whether that changes anything for Ollama specifically is unverified (`docs/Bugs.md` #53). `CODEX_SANDBOX` and `CODEX_APPROVAL_POLICY` are now named `config.py` constants (`"danger-full-access"` / `"untrusted"` by default) threaded through every Codex-harness backend — see `docs/Decisions.md` ADR-041. The planner can request a run-specific coding-agent roster from the configured small/medium and expert menus, capped by `MAX_CODING_AGENT_COUNT`. Coding tasks round-robin across that roster. If no valid roster is supplied, tasks fall back to the configurable slots A and B. Override them independently with `CODING_AGENT_A_BACKEND` / `CODING_AGENT_A_MODEL` and `CODING_AGENT_B_BACKEND` / `CODING_AGENT_B_MODEL`. The legacy shared `CODING_AGENT_BACKEND` / `CODING_AGENT_MODEL` variables remain valid fallbacks, so both slots can still be configured identically.

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
