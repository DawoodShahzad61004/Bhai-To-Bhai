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

`Orchestrate -> Merge -> Review` is the inner loop and runs once per dependency wave. The supervisor is outside that loop and evaluates the finished repository against the original requirements only after every wave is accepted. Reviewer rework resets the integration branch to the rejected wave's base and resumes the same coding sessions when the backend supports it. Supervisor replan resets integration to the run's original SHA and creates a new plan.

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

Every task in a wave branches from the same integration SHA. Threads are used because workers block in external subprocesses; concurrency happens in the child CLIs. A successful report with no Git-observed changes becomes `no_changes`. The filesystem, not the report, decides whether work occurred.

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
| `orchestrator/adapters/base.py` | Defines `AgentRequest`, normalized `AgentResult`, the closed error taxonomy, adapter selection, and the non-raising boundary used by every node. |
| `orchestrator/adapters/claude.py` | Runs Claude Code through stdin with JSON output, tool/budget controls, wall-clock timeout, telemetry, and session resume. |
| `orchestrator/adapters/codex.py` | Runs `codex exec` through stdin with workspace sandboxing and a dedicated final-answer file; reads errors from the end of stderr. |
| `orchestrator/adapters/maestro.py` | Runs synchronous `maestro delegate`, resolves the repo-local binary, and adds a wall-clock deadline above Maestro's stale-stream timeout. |
| `orchestrator/adapters/stub.py` | Provides deterministic scripted replies for offline end-to-end tests and deliberately fails unscripted tags. |

Transport and vendor are separate choices. Supported invocation modes are direct vendor CLIs, Maestro delegation, and the stub backend. Failures are classified as `not_installed`, `timeout`, `rate_limit`, `no_output`, `agent_error`, or `bad_request`; they return to the graph as data instead of raising through the router.

### Support Modules

| Module | Responsibility |
|---|---|
| `orchestrator/config.py` | Centralizes toggles, paths, transports, agent/model assignments, deadlines, concurrency, bounds, and worktree settings. |
| `orchestrator/artifacts.py` | Creates run directories and performs immediate atomic or append-only artifact writes. |
| `orchestrator/parsing.py` | Extracts and validates structured JSON from agent replies, including fenced output. |
| `orchestrator/logging_config.py` | Correlates console and persistent file logs by run id, including worker-thread context. |
| `orchestrator/preflight.py` | Standalone environment probe for agent binaries, Maestro, MongoDB, and Git worktrees; it is not called by `main.py`. |

## Configuration and Model Tiers

| Concern | Default |
|---|---|
| Invocation | `direct` |
| Reviewer / supervisor | enabled / enabled |
| Interactive requirements | enabled |
| Git worktrees | enabled |
| Parallel coding tasks | 3 |
| Rework rounds | 2 per wave |
| Replan rounds | 1 per run |
| Wave cap | 20 |
| Mechanical stages | Claude Haiku: requirements, wave orchestrator, merger |
| Judgment stages | Claude Sonnet: planner, reviewer, supervisor |
| Coding subagents | Claude Sonnet |

The roster is configurable per stage and may switch a stage to Codex. Gemini assignments in the design notes were translated to the installed Claude tiers because Gemini CLI was unavailable. The architectural rule remains: moving data and invoking deterministic operations uses the smaller tier; making correctness judgments uses the larger tier.

## Persistence, Recovery, and Audit

`main.py` compiles the graph with `SqliteSaver` and uses the run id as LangGraph's `thread_id`. A paused requirements question or interrupted process can resume with `--resume <run-id>` from `orchestrator/checkpoints/`. Append-only `events.jsonl`, `learnings.md`, and the run log preserve evidence even if the graph does not reach a terminal node.

Rejected work is reversible without being erased:

* Reviewer rework resets integration to the wave base SHA.
* Supervisor replan resets integration to the run base SHA.
* Task branches and vendor session identifiers remain available for inspection or same-session continuation.

Task claims are compared with Git diffs, conflict claims with index and marker scans, and final claims with requirement-linked review evidence.

## Testing and Operational Evidence

The orchestrator has **189 tests across 10 test files**. They cover configuration, graph wiring/toggles, CLI behavior, requirements interrupts, deterministic waves, dispatch/worktrees/reverts, merging, reviewing, supervision, and terminal bounds. The full suite was recorded passing on 2026-08-07; workflow tests use the first-class stub transport so they are deterministic and incur no agent cost.

`orchestrator/run_logs/live_probe_20260807_194239.debug.log` records a real Claude adapter probe: one turn, structured output parsed, session id captured, approximately 4.8 seconds, and `$0.067745`. There has not yet been a paid six-agent end-to-end run; worktree merge, rework, replan, and recovery are validated by the stub-backed suite rather than a paid live target.

Known open findings are tracked in `docs/Bugs.md` #26–#28: incomplete merge-context propagation, duplicate requirements routing logic, and successful-run worktree cleanup.

## Technology Stack

| Layer | Technology |
|---|---|
| Workflow and checkpointing | LangGraph + SQLite checkpointer |
| Agent execution | Claude Code CLI, Codex CLI, Maestro delegation, scripted stub |
| State and validation | Python typed state, deterministic reducers, structured JSON parsing |
| Isolation and integration | Git branches and worktrees |
| Persistence and audit | Markdown/JSON artifacts, JSONL events, per-run DEBUG logs |
| Environment probe | Git, agent binaries, repo-local Maestro, MongoDB connectivity |
| Tests | pytest, 189 tests |

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
