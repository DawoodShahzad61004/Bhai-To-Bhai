## System Overview

`Bhai-To-Bhai` is a LangGraph controller for a six-stage software-delivery pipeline that operates on an external Git repository. It coordinates existing coding-agent CLIs, persists each run as artifacts and checkpoints, isolates parallel coding tasks in Git worktrees, and accepts work only after merge and review evidence has been evaluated.

The production implementation is `orchestrator/`. The `yt_tutorial/` tree remains a separate learning sandbox and is not imported by the production pipeline.

The runtime separates **judgment** from **mechanics**. Requirements interpretation, planning, coding, review, and supervision use judgment-capable agents. Scheduling, routing, bounds, Git observation, merge checks, checkpointing, and artifact writes are deterministic code. Only reviewer and supervisor judgments may send the graph backward.

## High-Level Architecture

```text
START
  |
  v
Requirements survey -- questions? --> Requirements clarification
  |                                      |
  +--------------------------------------+
  v
Planner -- tasks + dependency edges --> deterministic wave schedule
  |
  v
Wave orchestrator -- parallel coding agents in isolated worktrees
  |
  v
Merger -- sequential integration + conflict verification
  |
  v
Reviewer -- rework --> Wave orchestrator
  | approve
  v
More waves? -- yes --> Wave orchestrator
  | no
  v
Supervisor -- replan --> Planner
  | accept / escalate
  v
END
```

`Orchestrate -> Merge -> Review` is the inner loop and runs once per dependency wave. The supervisor is outside that loop and evaluates the finished repository against the original requirements only after every wave is accepted. Reviewer rework currently resets the integration branch to the rejected wave's base and resumes the same coding sessions when the backend supports it. The reset is wave-wide: work from tasks that completed successfully in that attempt is removed from integration together with rejected work, although the task branches survive. Selectively retaining completed task results across attempts is planned but not implemented (`Bugs.md` #35). Supervisor replan resets integration to the run's original SHA and creates a new plan.

Reviewer and supervisor are optional quality gates. `ENABLE_REVIEWER=False` or `ENABLE_SUPERVISOR=False` removes the node from the compiled graph rather than inserting a pass-through. Routers return semantic outcomes such as `rework`, `next_wave`, and `done`; `graph.py` maps those outcomes to the topology that actually exists.

## Runtime Data Model

`PipelineState` in `orchestrator/state.py` is the checkpointed record of one run. It carries run identity, target and artifact paths, requirements, tasks, waves, the current cursor, worktrees, integration branch, agent sessions, results, verdicts, counters, events, cost, and terminal status.

Two reducers preserve state across nodes:

* `events` is append-only.
* `wave_results` is a keyed upsert on `(wave, attempt)`, because dispatch creates an attempt record and merge/review later update that same logical record.

Run status is explicit: `running`, `completed`, `bounded`, or `failed`. A termination guard firing is recorded as `bounded`, never as successful completion.

## Artifact and Repository Boundaries

The orchestrator repository owns control-plane state; the target repository owns product code.

```text
orchestrator/
  checkpoints/                 SQLite LangGraph checkpoints
  run_logs/                    one persistent DEBUG log per run
  runs/<run-id>/
    context.md                 researched task understanding
    user_choices.md            only explicit user choices and answers
    plan.json                  normalized plan and dependency graph
    TASK-*.json                one contract per coding task
    learnings.md               append-only findings
    events.jsonl               append-only audit events
    reviews/
      wave-*-attempt-*.md      per-wave reviewer evidence
      supervisor-*.md          final requirement audit

<target-parent>/.bhai-worktrees/
  <run>-<task>/                isolated task checkouts

<target-repository>/
  bhai/<run-id>/integration    provisional integrated result
  bhai/<run-id>/<task-id>      inspectable task branches
```

Artifacts use absolute paths and are written immediately in UTF-8. They live outside the target repository so an agent checkout or reset cannot erase the controller's memory. `user_choices.md` is written by deterministic orchestrator code from the questions asked and answers received; model assumptions and code-derived conclusions are excluded.

The current boundary makes artifacts outside the target checkout, but no longer makes them unreachable. Agents receive absolute artifact paths rather than pasted artifact contents, and the direct Claude/Codex adapters grant access to the run directory with `extra_dirs` / `--add-dir`. Planner, merger, reviewer, supervisor, and coding briefs tell agents to read `context.md`, `user_choices.md`, `plan.json`, and/or `learnings.md` by path as needed. Coding agents can append findings directly to the run's shared `learnings.md` through `python orchestrator/artifacts.py append-learning <run_dir> <agent> <message>`, which uses an OS-level sidecar lock so concurrent writers do not interleave while ordinary reads remain non-blocking. `user_choices.md` remains deterministic orchestrator-owned provenance. The artifacts are still scoped to one run rather than one target project; project-scoped shared context remains planned but not implemented (`Bugs.md` #36).

## Module Breakdown

### Entry Point and Graph Assembly

| Module | Responsibility |
|---|---|
| `orchestrator/main.py` | Parses `--goal`, `--target`, `--run-id`, `--resume`, `--dry-run`, and `--yes`; validates the external target; creates or resumes a run; handles interrupts; reports the terminal result. |
| `orchestrator/graph.py` | Adds enabled nodes, wires conditional edges, advances the wave cursor, and compiles the graph with a checkpointer. |
| `orchestrator/routes.py` | Converts state into semantic routing keys without naming graph nodes or duplicating toggle logic. |
| `orchestrator/state.py` | Defines `PipelineState`, reducers, attempt records, terminal statuses, and initial state. |

`main.py` is the only executable entry. Everything else under `orchestrator/` is library code.

### Requirements

| Module | Responsibility |
|---|---|
| `orchestrator/requirements/node.py` | Surveys the target read-only, writes `context.md` and explicit choices, pauses for material questions, records answers verbatim, and resumes the same requirements session where possible. |
| `orchestrator/requirements/prompts.py` | Defines the survey and clarification contracts and structured output schemas. |

Survey and clarification are separate graph nodes because LangGraph re-executes an interrupted node from its first line on resume. Splitting the work prevents a paid survey call from running twice. Questions are search-first and limited to decisions the repository cannot answer.

### Planner

| Module | Responsibility |
|---|---|
| `orchestrator/planner/node.py` | Calls the planner, normalizes task output, persists `plan.json` and task files, and handles full replans. |
| `orchestrator/planner/waves.py` | Validates dependencies and derives waves deterministically with Kahn's algorithm; rejects dangling dependencies and cycles. |
| `orchestrator/planner/prompts.py` | Defines the planning and replanning contracts. |

The model decides task boundaries and dependency edges because those require judgment. Code derives the schedule because grouping independent tasks into waves is arithmetic and a correctness property.

### Wave Orchestration and Coding Dispatch

| Module | Responsibility |
|---|---|
| `orchestrator/wave_orchestrator/node.py` | Runs one wave, records its base SHA, prepares rework by resetting rejected integration, and stops a wave in which every task failed. |
| `orchestrator/wave_orchestrator/dispatch.py` | Creates task worktrees, submits all tasks before collecting results, commits residual changes, and records agent claims separately from Git-observed files. |
| `orchestrator/wave_orchestrator/prompts.py` | Builds first-attempt and same-session rework briefs. |
| `orchestrator/worktrees.py` | Wraps Git/worktree operations in non-raising results and manages branches, resets, stale paths, and cleanup. |

Every task in a wave branches from the same integration SHA. Threads are used because workers block in external subprocesses; concurrency happens in the child CLIs. The planner may now size a run-specific `coding_agents` roster, choosing one to `MAX_CODING_AGENT_COUNT` allowed backend/model pairs from the configured small/medium and expert menus. The wave orchestrator dispatches tasks round-robin across that roster, with the offset advanced across prior waves so roster order remains meaningful over the whole plan. If the planner supplies no valid roster, dispatch falls back to the default `CODING_AGENT_A` / `CODING_AGENT_B` pair. `MAX_PARALLEL_TASKS` remains the independent thread cap. A successful report with no Git-observed changes becomes `no_changes`. The filesystem, not the report, decides whether work occurred.

### Merger

| Module | Responsibility |
|---|---|
| `orchestrator/merger/node.py` | Selects successful task results, invokes the merge engine, and folds the merge record into the current wave attempt. |
| `orchestrator/merger/merge.py` | Merges task branches sequentially, skips failed tasks, invokes an agent only for conflicts, and aborts unverifiable resolutions. |
| `orchestrator/merger/prompts.py` | Defines the conflict-resolution brief. |

Clean merges make no model call. A claimed conflict resolution is accepted only when Git reports no unmerged paths and a disk scan finds no conflict markers before staging.

### Reviewer

| Module | Responsibility |
|---|---|
| `orchestrator/reviewer/node.py` | Reviews one merged wave against its task contracts using claims plus observed files, writes evidence-bearing notes, approves, or requests actionable rework. |
| `orchestrator/reviewer/prompts.py` | Defines the reviewer evidence and verdict schema. |

Reviewer rework is bounded per wave. Exhausting `MAX_REWORK_ROUNDS` ends the run as `bounded`. Review is task-attributed; integration-wide findings belong to the merge/integration path rather than an arbitrary coding agent.

### Supervisor

| Module | Responsibility |
|---|---|
| `orchestrator/supervisor/node.py` | Performs an independent requirement-to-evidence audit after all waves, accepts the run, requests a full replan, or escalates misunderstood requirements to the user. |
| `orchestrator/supervisor/prompts.py` | Defines the final audit and replan feedback schema. |

Supervisor replans are bounded by `MAX_REPLAN_ROUNDS`. Exhaustion records `bounded`; acceptance records `completed`.

### Agent Adapters

| Module | Responsibility |
|---|---|
| `orchestrator/adapters/base.py` | Defines `AgentRequest`, normalized `AgentResult`, the closed error taxonomy, adapter selection, the non-raising boundary used by every node, a scrubbed UTF-8 subprocess environment (`subprocess_env()`), and `run_with_deadline()`, which terminates the complete Windows CLI process tree on timeout. |
| `orchestrator/adapters/claude.py` | Runs Claude Code through stdin with JSON output, tool/budget controls, wall-clock timeout, telemetry, and vendor session resume (sessions are persisted rather than suppressed on cold start, so `--resume` has something to resume). |
| `orchestrator/adapters/codex.py` | Runs `codex exec` through stdin with workspace sandboxing and a dedicated final-answer file; reads errors from the end of stderr, and parses `--json` events for a resumable thread id. |
| `orchestrator/adapters/maestro.py` | Runs synchronous `maestro delegate`, resolves the repo-local binary, and adds a wall-clock deadline above Maestro's stale-stream timeout. |
| `orchestrator/adapters/ollama.py` | Routes a `backend="ollama"` dispatch unconditionally through `adapters/codex.py`'s `run_codex(local_provider="ollama")`, since bare `ollama run` has no file/shell/sandbox/session tooling of its own and can only supply inference underneath Codex's existing agent-loop machinery. Sets `-c model_reasoning_effort=none` because Codex otherwise assumes reasoning-capable models, which Ollama's non-thinking models (Qwen 2.5 Coder, Devstral) reject. |
| `orchestrator/adapters/stub.py` | Provides deterministic scripted replies for offline end-to-end tests and deliberately fails unscripted tags. |

Transport and vendor are separate choices. Supported invocation modes are direct vendor CLIs (including locally-hosted Ollama models via the Codex harness), Maestro delegation, and the stub backend. Failures are classified as `not_installed`, `timeout`, `rate_limit`, `no_output`, `agent_error`, or `bad_request`; they return to the graph as data instead of raising through the router.

### Support Modules

| Module | Responsibility |
|---|---|
| `orchestrator/config.py` | Centralizes toggles, paths, transports, agent/model assignments, deadlines, concurrency, bounds, and worktree settings. |
| `orchestrator/artifacts.py` | Creates run directories and performs immediate atomic or append-only artifact writes. |
| `orchestrator/parsing.py` | Extracts and validates structured JSON from agent replies, including fenced output. |
| `orchestrator/logging_config.py` | Correlates console and persistent file logs by run id, including worker-thread context. |
| `orchestrator/preflight.py` | Standalone environment probe for agent binaries, Maestro (resolved through the same local-install precedence as `adapters/maestro.py`), MongoDB, and Git worktrees; it is not called by `main.py`. |

## Configuration and Model Tiers

| Concern | Default |
|---|---|
| Invocation | `direct` |
| Reviewer / supervisor | enabled / enabled |
| Interactive requirements | enabled |
| Git worktrees | enabled |
| Parallel coding tasks | 3 |
| Max planner-sized coding roster | 5 |
| Rework rounds | 2 per wave |
| Replan rounds | 1 per run |
| Wave cap | 20 |
| Mechanical stages | Claude Haiku: requirements, wave orchestrator, merger |
| Planning stage | Codex CLI default: planner |
| Judgment stages | Claude Sonnet: reviewer, supervisor |
| Coding roster menus | Small/medium: Claude Haiku. Expert: Claude Sonnet, Codex CLI default. |
| Fallback coding subagent A / B | Codex CLI default / Claude Sonnet |

The roster is configurable per stage. The 2026-08-09 defaults are deliberately mixed after two rapid operational flips: requirements, wave orchestration, and merge use Claude Haiku; planning remains on Codex; review and supervision use Claude Sonnet. Coding slots have independent `CODING_AGENT_A_*` and `CODING_AGENT_B_*` overrides and fall back to the legacy shared `CODING_AGENT_BACKEND` / `CODING_AGENT_MODEL` variables for backward compatibility. Since 2026-08-10, those slots are fallback defaults rather than the only coding roster: the planner can request a larger or smaller validated roster for the run. Gemini assignments in the design notes were translated to installed vendors because Gemini CLI was unavailable. The architectural rule remains: moving data and invoking deterministic operations uses the smaller tier; making correctness judgments uses the larger tier.

Any role's `backend` may be set to `"ollama"` with a locally-hosted model as `model`, which `adapters/ollama.py` runs through the Codex harness (ADR-028). This was exercised on 2026-08-11 as a zero-marginal-cost fallback during a Claude weekly rate-limit exhaustion. It is reliable for single-shot structured-output stages (requirements, planning-shaped JSON) but not yet proven reliable for coding-agent dispatch itself — see `Bugs.md` #40–#41 and `Research.md` topics 32–33. There is deliberately no per-harness switch for it: a short-lived `OLLAMA_HARNESS` option that could route through Claude Code instead of Codex was removed the same day it failed in production, since Claude Code's `--model` flag has no mechanism for accepting an arbitrary local model tag (`Bugs.md` #39).

## Persistence, Recovery, and Audit

`main.py` compiles the graph with `SqliteSaver` and uses the run id as LangGraph's `thread_id`. A paused requirements question or interrupted process can resume with `--resume <run-id>` from `orchestrator/checkpoints/`. Append-only `events.jsonl`, `learnings.md`, and the run log preserve evidence even if the graph does not reach a terminal node.

Rejected work is reversible without being erased:

* Reviewer rework resets integration to the wave base SHA.
* Supervisor replan resets integration to the run base SHA.
* Task branches and vendor session identifiers remain available for inspection or same-session continuation.

Task claims are compared with Git diffs, conflict claims with index and marker scans, and final claims with requirement-linked review evidence.

## Testing and Operational Evidence

The orchestrator has **209 tests across 10 test files**. They cover configuration, graph wiring/toggles, CLI behavior, requirements interrupts, deterministic waves, dispatch/worktrees/reverts, merging, reviewing, supervision, terminal bounds, subprocess environment scrubbing, vendor session-id extraction, planner-sized coding rosters, concurrent direct `learnings.md` appends, and Ollama-harness routing through Codex. The full suite was recorded passing on 2026-08-07 (189 tests), 2026-08-08 after the adapter session-resume fixes (193 tests), 2026-08-09 after the process-tree deadline and dual coding-slot changes (197 tests), 2026-08-10 after the prompt/reference and roster work (197 tests), and 2026-08-11 after the Ollama adapter landed and its short-lived Claude-harness option and three accompanying tests were removed the same day (209 tests). Workflow tests use the first-class stub transport so they are deterministic and incur no agent cost.

`orchestrator/run_logs/live_probe_20260807_194239.debug.log` records a real Claude adapter probe: one turn, structured output parsed, session id captured, approximately 4.8 seconds, and `$0.067745`. Two full live runs against real target repositories were diagnosed read-only on 2026-08-08 (`Research.md` topic 24): one failed at worktree setup against an uncommitted target repository, the other completed and was accepted by the supervisor but contained a latent rendering defect the pipeline's checks did not cover. On 2026-08-10, `run-20260810-162135` proved the new path-reference and direct-learning flow in a paid smoke run: the planner chose three Haiku coding agents, all three task prompts used artifact paths and the append-learning command, reviewer rework fixed an over-line-limit `data.js`, and the supervisor accepted the run. Later pricing-tool runs exposed two operational limits: a stale `sonnet-5` menu entry caused an immediate agent error (`Bugs.md` #37, fixed by using live aliases), and a single-Sonnet plan then hit the user's weekly Claude limit before producing usable wave output (`Bugs.md` #38). There has not yet been a paid six-agent end-to-end run that is both live and defect-free; worktree merge, rework, replan, and recovery remain primarily validated by the stub-backed suite.

Known open findings are tracked in `docs/Bugs.md` #26–#28 (incomplete merge-context propagation, duplicate requirements routing logic, and successful-run worktree cleanup), #32–#33 (unborn-repository worktree setup and a browser-rendered HTML defect), #35–#36 (wave-wide rework discarding completed integrations, and run-scoped artifacts remaining non-project-scoped), #38 (static roster validation does not yet route around live quota exhaustion), #40 (a local Ollama model breaking the pipeline's JSON-only output contract), and #41 (local coding-agent models reporting false task completion and, once, fabricating a shared `learnings.md` entry — a model-capability limit, not an adapter defect).

## Technology Stack

| Layer | Technology |
|---|---|
| Workflow and checkpointing | LangGraph + SQLite checkpointer |
| Agent execution | Claude Code CLI, Codex CLI, locally-hosted Ollama models (via the Codex harness), Maestro delegation, scripted stub |
| State and validation | Python typed state, deterministic reducers, structured JSON parsing |
| Isolation and integration | Git branches and worktrees |
| Persistence and audit | Markdown/JSON artifacts, JSONL events, per-run DEBUG logs |
| Environment probe | Git, agent binaries, repo-local Maestro, MongoDB connectivity |
| Tests | pytest, 209 tests |

The runtime dependency list is intentionally small. Agents are external subprocesses, so the project does not need model SDKs. `pymongo` exists for the standalone preflight probe, not for pipeline storage.

## Changelog

### 2026-07-31 — Target architecture documented

The initial documents defined cross-agent routing, canonical context, resumable vendor sessions, worktree isolation, deterministic checks, and auditability before production code existed.

---

### 2026-08-03 — Maestro selected and handoff mechanics specified

Maestro became the external-agent delegation layer; durable entry-time handoff reconstruction and a configured budget ledger became the intended continuity mechanisms.

---

### 2026-08-04 — Preflight and learning sandbox added

The first production-track file was `orchestrator/preflight.py`. A separate LangGraph tutorial track was opened to learn routing and supervision without coupling experiments to the real system.

---

### 2026-08-05 — Tutorial graph executed end to end

Running the sandbox exposed routing, termination, transport, logging, and artifact-verification failures that later became explicit production constraints.

---

### 2026-08-06 — Real CLI adapters proven in the sandbox

Claude Code and Codex were invoked directly and concurrently. The experiments established stdin prompt delivery, explicit final-answer channels, wall-clock deadlines, non-raising adapter boundaries, and filesystem evidence over self-report.

---

### 2026-08-07 — Six-stage production orchestrator implemented

The design was researched with ChatGPT and Claude, specified in temporary pipeline notes, and implemented as the complete `orchestrator/` runtime: requirements, planner, wave orchestrator, merger, reviewer, supervisor, adapters, Git worktrees, artifacts, semantic routing, checkpoint/resume support, bounds, and audit logging.

The implementation commit is recorded by Git as `Workflow implemented` with **8,888 insertions across 53 files**; its timestamp crossed midnight locally, while the work and chat history belong to the August 7 session. The project added `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, 10 test files, and **189 passing tests**. The temporary architecture notes were retired after their decisions and rationale were incorporated into the permanent documentation.

---

### 2026-08-08 — Windows-adapter regressions and vendor session resume fixed; two live runs diagnosed

Two Windows-specific defects in the adapter layer were fixed: Maestro binary resolution in `preflight.py` finally adopted the precedence `adapters/maestro.py` already used, and a new scrubbed subprocess environment (`adapters/base.py::subprocess_env()`) closed a `_bz2` import failure traced to inherited Python environment variables crossing a Windows console-script launcher boundary. Vendor session resume, previously non-functional on both Codex (a session id was captured but the session itself was discarded via `--ephemeral`) and Claude Code (`--no-session-persistence` returned a session id `--resume` then rejected outright), was proven broken empirically on both and fixed on both, so the reviewer's rework loop can now actually resume the agent that made a mistake rather than cold-starting or failing. Two real runs against external target repositories were separately diagnosed read-only, surfacing an unborn-repository worktree-setup failure and a browser-visible HTML defect the reviewer's checks did not cover. The test suite grew from 189 to 193 tests.

---

### 2026-08-09 — Dual coding slots added; Windows CLI deadlines made process-tree safe; parallel-run gaps recorded

Commit `4a123eb` (`Implement double coding subagents`) added two independently configurable coding slots and stable A/B assignment by task index, rebalanced five of six orchestration roles from Codex back to Claude while leaving planning on Codex, and moved all three vendor adapters onto the shared `run_with_deadline()` helper. On Windows, the helper creates a new process group and uses `taskkill /F /T /PID` after a timeout so the npm `.cmd` wrapper and its `node.exe` descendants release inherited pipes; this closes the hang that left requirements runs permanently silent after their nominal 300-second deadline (`Bugs.md` #34). The full suite grew to 197 passing tests. A nine-task parallel stress run separately confirmed two open design gaps: a rejected attempt resets the whole wave's integrated work, and task worktrees cannot directly access the canonical run artifacts, which are pasted into prompts and remain run-scoped (`Bugs.md` #35–#36). Commit `28b3aa4` then stored two temporary Bhai Digital Studio prompts, including a MongoDB/Express parallel-dispatch stress case, for repeatable manual runs.

---

### 2026-08-10 - Planner-sized rosters, path-reference prompts, and direct run-local learnings

Commit `436fb91` (`Enhance coding agent management and prompts in the orchestrator`) let the planner request a run-specific coding-agent roster, validated by `normalise_coding_agents()` against configured small/medium and expert model menus and capped at five agents. The wave orchestrator now dispatches against that roster with a run-wide round-robin offset, falling back to the old A/B coding slots only when no valid roster is supplied.

The same change replaced full artifact injection with file-path references across planner, coding, merger, reviewer, and supervisor prompts. Direct Claude/Codex adapters gained `extra_dirs` / `--add-dir` so agents can read the run directory from isolated worktrees, and `artifacts.py` gained an OS-locked `append-learning` CLI so coding agents can write `learnings.md` directly while other agents read it. The test suite remained at 197 tests and now covers roster validation plus real concurrent learning appends.

Live runs confirmed both the success path and remaining boundaries. `run-20260810-162135` completed and was accepted after reviewer rework, proving three Haiku coding agents could read artifacts by path and append learnings directly. `run-20260810-164949` exposed a stale `sonnet-5` model alias in the menu, fixed by switching to live aliases. `run-20260810-171145` then exposed the separate unsolved problem of live quota exhaustion: a valid `sonnet` agent can still be unavailable at dispatch time, so the budget/cooldown router remains future work.

---

### 2026-08-11 — Ollama adapter added as a zero-cost fallback; production evidence that the limit is model capability, not the pipeline

Hitting Claude's weekly rate limit at the very start of the session (`run-20260811-144518`, `-145041`) motivated `orchestrator/adapters/ollama.py`: bare `ollama run` has no file/shell/sandbox/session tooling, so it routes every `backend="ollama"` dispatch through Codex's existing agent-loop machinery via `run_codex(local_provider="ollama")`, with `-c model_reasoning_effort=none` because Codex otherwise assumes reasoning-capable models and Ollama's non-thinking models reject that mode. A same-day config option letting this routing choose Claude Code's harness instead of Codex's was proven broken in production within hours (`run-20260811-174803`: Claude Code's `--model` flag cannot accept an arbitrary local model tag) and removed outright rather than repaired — Codex is now the only Ollama harness (ADR-028, `Bugs.md` #39).

The adapter itself works: two runs completed the requirements stage cleanly through Ollama-via-Codex, at 142–347s depending on model size, with cosmetic (non-blocking) Codex/Ollama model-metadata warnings on every call. It does not yet make locally-hosted models reliable coding agents. Given real multi-turn coding-task dispatch rather than single-shot JSON generation, both Devstral and Qwen 2.5 Coder reported false task completion with zero files changed, and one run's Qwen agent both refused tool use outright and left a fabricated success entry in the run's shared `learnings.md` — a new failure class, since it is the first case in this project of a false claim entering the shared cross-agent evidence channel rather than only a model's own turn (`Bugs.md` #40–#41, `Research.md` topics 32–33). None of this was accepted by the pipeline: the reviewer's evidence-over-claims discipline (ADR-019/ADR-020) caught every instance by comparing claims against Git-observed files and by running the code rather than reading the report. The session's own conclusion, recorded in the repository's `Ollama Coding Agent Working.md`, is that the ceiling is model scale on the available hardware rather than a defect in the orchestrator — a ~70B-class coding-finetuned model is the untested next step. The suite grew from 197 to **209 tests** (new Ollama-adapter tests added, three since-removed Claude-harness tests subtracted).
