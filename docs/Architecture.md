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

Artifacts use absolute paths and are written immediately in UTF-8. The target repository now owns project-scoped shared memory under `runs/`: `context.md` is the current project snapshot, `learnings.md` is an append-only cross-run channel with a sidecar lock, and `user_choices.md` is deterministic orchestrator-owned provenance. Plans, task contracts, reviews, and events remain under run-specific paths so every execution retains an attributable audit trail. Legacy flat artifacts migrate safely, precise local excludes prevent accidental commits, and the first run creates valid empty shared files before any node reads them.

Agents receive absolute artifact paths rather than pasted artifact contents. Direct Claude, Codex, and Copilot adapters grant access to the target repository's shared directory with `extra_dirs` / `--add-dir`; planner, merger, reviewer, supervisor, and coding briefs tell agents which shared or run-specific files to read. Coding agents can append findings directly through `python orchestrator/artifacts.py append-learning <run_dir> <agent> <message>`, with OS-level locking so concurrent writers do not interleave. This fixes the project-scope portion of `Bugs.md` #36 while preserving per-run plans, tasks, reviews, and events.

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
| `orchestrator/adapters/copilot.py` | Runs GitHub Copilot CLI non-interactively through `copilot -p`, parses JSONL output, supports resume/model/workspace flags, and preserves stderr-only authentication or service failures as classified `AgentResult` errors. |
| `orchestrator/adapters/local_llm.py` | Registers `direct:local_llm`, loads `CUSTOM_API_*` configuration, and keeps Codex as the coding-agent runtime while redirecting inference to a configurable OpenAI-compatible local server. |
| `orchestrator/adapters/local_llm_bridge.py` | Translates a Chat Completions-only local server into the minimal Responses stream Codex consumes, including consolidated system/developer instructions, tool-name normalization, SSE/event synthesis, and compact request shaping for smaller context windows. |
| `orchestrator/adapters/maestro.py` | Runs synchronous `maestro delegate` when `INVOCATION=maestro`; the adapter remains available, but the repository no longer bundles a local `maestro-flow` dependency, so callers must provide the binary explicitly via `MAESTRO_BIN` or `PATH`. |
| `orchestrator/adapters/ollama.py` | Routes a `backend="ollama"` dispatch unconditionally through `adapters/codex.py`'s `run_codex(local_provider="ollama")`, since bare `ollama run` has no file/shell/sandbox/session tooling of its own and can only supply inference underneath Codex's existing agent-loop machinery. Sets `-c model_reasoning_effort=none` because Codex otherwise assumes reasoning-capable models, which Ollama's non-thinking models (Qwen 2.5 Coder, Devstral) reject. |
| `orchestrator/adapters/stub.py` | Provides deterministic scripted replies for offline end-to-end tests and deliberately fails unscripted tags. |

Transport and vendor are separate choices. Supported invocation modes are direct vendor CLIs (Claude, Codex, Copilot, locally-hosted Ollama models via the Codex harness, and arbitrary OpenAI-compatible local servers via the separate `direct:local_llm` bridge), optional Maestro delegation, and the stub backend. Failures are classified as `not_installed`, `timeout`, `rate_limit`, `no_output`, `agent_error`, or `bad_request`; they return to the graph as data instead of raising through the router.

### Support Modules

| Module | Responsibility |
|---|---|
| `orchestrator/config.py` | Centralizes toggles, paths, transports, agent/model assignments, deadlines, concurrency, bounds, and worktree settings. |
| `orchestrator/artifacts.py` | Creates run directories and performs immediate atomic or append-only artifact writes. |
| `orchestrator/parsing.py` | Extracts and validates structured JSON from agent replies, including fenced output. |
| `orchestrator/logging_config.py` | Correlates console and persistent file logs by run id, including worker-thread context. |
| `orchestrator/preflight.py` | Standalone environment probe for agent binaries, optional Maestro resolution, MongoDB, and Git worktrees; it is not called by `main.py`. |
| `orchestrator/process_trace.py` | Windows-focused subprocess-tree tracing utilities used to capture parent/child lifecycle evidence during orphan-process and timeout investigations without changing the normal adapter path. |

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
| Mechanical stages | `direct:local_llm` `QuantTrio/Qwen3.6-27B-AWQ`: requirements, merger; Copilot (auto): wave orchestrator |
| Planning stage | `direct:local_llm` `QuantTrio/Qwen3.6-27B-AWQ`: planner |
| Judgment stages | `direct:local_llm` `QuantTrio/Qwen3.6-27B-AWQ`: reviewer, supervisor |
| Coding roster menus | Small/medium: Copilot (auto), `direct:local_llm` `QuantTrio/Qwen3.6-27B-AWQ`, plus the configured Ollama cloud/local candidates. Expert: Codex CLI default. |
| Fallback coding subagent A / B | Codex CLI default / Codex CLI default |

The roster is configurable per stage. The current working configuration reflects the Aug 18 local-server shift: the default requirements, planner, merger, reviewer, and supervisor roles all route through `direct:local_llm`, which still uses Codex as the coding-agent harness under the hood; Copilot remains configured for the wave-orchestrator role; and the planner may choose from the active small/medium and expert menus for coding-task dispatch. Coding slots retain independent `CODING_AGENT_A_*` and `CODING_AGENT_B_*` overrides plus legacy shared fallbacks. The architectural rule remains: moving data and invoking deterministic operations uses the smaller tier; making correctness judgments uses the larger tier, but the underlying transport may still be one shared harness when that is what preserves the required file/shell/session behavior.

Any role's `backend` may be set to `"ollama"` with a locally-hosted model as `model`, which `adapters/ollama.py` runs through the Codex harness (ADR-028). This was exercised on 2026-08-11 as a zero-marginal-cost fallback during a Claude weekly rate-limit exhaustion. It is reliable for single-shot structured-output stages (requirements, planning-shaped JSON) but not yet proven reliable for coding-agent dispatch itself - see `Bugs.md` #40-#41 and `Research.md` topics 32-33. There is deliberately no per-harness switch for it: a short-lived `OLLAMA_HARNESS` option that could route through Claude Code instead of Codex was removed the same day it failed in production, since Claude Code's `--model` flag has no mechanism for accepting an arbitrary local model tag (`Bugs.md` #39).

The separate `backend="local_llm"` path (ADR-033) is not the Ollama backend renamed. It keeps Codex as the agent runtime but redirects inference to a configurable OpenAI-compatible local server. Where that server only exposes Chat Completions, `local_llm_bridge.py` synthesizes the minimal Responses stream Codex consumes, preserving file access, shell execution, sandboxing, and session capture rather than falling back to a plain model client that would lose those properties.

## Persistence, Recovery, and Audit

`main.py` compiles the graph with `SqliteSaver` and uses the run id as LangGraph's `thread_id`. A paused requirements question or interrupted process can resume with `--resume <run-id>` from `orchestrator/checkpoints/`. Append-only `events.jsonl`, `learnings.md`, and the run log preserve evidence even if the graph does not reach a terminal node.

Rejected work is reversible without being erased:

* Reviewer rework resets integration to the wave base SHA.
* Supervisor replan resets integration to the run base SHA.
* Task branches and vendor session identifiers remain available for inspection or same-session continuation.

Task claims are compared with Git diffs, conflict claims with index and marker scans, and final claims with requirement-linked review evidence.

## Testing and Operational Evidence

The orchestrator reached **231 passing tests across 11 test files** in the Aug 18 local-LLM validation session, run through the repository virtual environment with an isolated pytest temp directory. Coverage includes configuration, graph wiring/toggles, CLI behavior, requirements interrupts, deterministic waves, dispatch/worktrees/reverts, merging, reviewing, supervision, terminal bounds, subprocess environment scrubbing, vendor session-id extraction, Copilot parsing/classification, planner-sized coding rosters, concurrent direct `learnings.md` appends, target-repository artifact migration, Windows process tracing, Ollama-harness routing through Codex, and the local Chat Completions->Responses bridge used by `direct:local_llm`. A fresh Aug 21 pytest rerun from this checkout still hit the long-standing Windows temp-directory permission cleanup issue at session finish, so the current turn's direct verification is `git diff --check` plus that reproduced cleanup failure rather than a new full green suite. Workflow tests use the first-class stub transport so they are deterministic and incur no agent cost.

`orchestrator/run_logs/live_probe_20260807_194239.debug.log` records a real Claude adapter probe: one turn, structured output parsed, session id captured, approximately 4.8 seconds, and `$0.067745`. Two full live runs against real target repositories were diagnosed read-only on 2026-08-08 (`Research.md` topic 24): one failed at worktree setup against an uncommitted target repository, the other completed and was accepted by the supervisor but contained a latent rendering defect the pipeline's checks did not cover. On 2026-08-10, `run-20260810-162135` proved the new path-reference and direct-learning flow in a paid smoke run: the planner chose three Haiku coding agents, all three task prompts used artifact paths and the append-learning command, reviewer rework fixed an over-line-limit `data.js`, and the supervisor accepted the run. The Aug 18 work then added three more live adapter-level evidence points: the real Copilot nested-event parser fix (`Bugs.md` #46), a confirmed stage-schema failure mode distinct from transport success (`Bugs.md` #47), and a successful end-to-end local-server Codex-harness turn after the Responses bridge and request compaction landed (`Research.md` topic 40). There has not yet been a paid six-agent end-to-end run that is both live and defect-free; worktree merge, rework, replan, and recovery remain primarily validated by the stub-backed suite.

Known open findings are tracked in `docs/Bugs.md` #26-#28, #32-#33, #35, #38, #40-#43, and #47. #36's project-scope artifact gap is fixed. #42 remains the external GitHub/Copilot service-validation risk, #43 tracks subscription-gated Ollama Cloud roster entries, and #44 is now closed by the separate `direct:local_llm` backend rather than by broadening the Ollama bridge. The current Ollama backend remains its own Codex harness route, distinct from the direct local-server path.

## Technology Stack

| Layer | Technology |
|---|---|
| Workflow and checkpointing | LangGraph + SQLite checkpointer |
| Agent execution | Claude Code CLI, Codex CLI, GitHub Copilot CLI, locally-hosted Ollama models (via the Codex harness), configurable OpenAI-compatible local servers (via the `direct:local_llm` Codex harness bridge), optional Maestro delegation, scripted stub |
| State and validation | Python typed state, deterministic reducers, structured JSON parsing |
| Isolation and integration | Git branches and worktrees |
| Persistence and audit | Markdown/JSON artifacts, JSONL events, per-run DEBUG logs |
| Environment probe | Git, agent binaries, optional Maestro binary, MongoDB connectivity |
| Tests | pytest, 231 passing tests |

The runtime dependency list is intentionally small. Agents are external subprocesses, so the project does not need model SDKs. `pymongo` exists for the standalone preflight probe, not for pipeline storage.

## Changelog

### 2026-08-18 - Process tracing, Copilot parser/schema findings, Graphify-first cleanup, and the direct local-LLM backend

Commit `75b0000` added `orchestrator/process_trace.py` plus focused tests so Windows process-tree investigations can be driven by lifecycle evidence instead of post-hoc process listings. This complemented the earlier deadline fix rather than replacing it.

Commit `7e59d42` fixed a second Copilot adapter bug that only appeared once GitHub recovered enough to return a real reply: the successful final message was nested under `assistant.message -> data.content`, while the parser still only searched top-level reply fields. The same Aug 18 chat then established a separate, still-open boundary: Copilot's JSON event transport does not itself guarantee the stage's structured-output contract, so a successful turn can still return prose that the orchestrator must reject (`Bugs.md` #46-#47, `Research.md` topic 39).

Commit `e71b390` removed the repo-local `maestro-flow` packaging experiment from `package.json` / `package-lock.json`, strengthened Graphify-first instructions in `AGENTS.md`, `CLAUDE.md`, and requirements prompts, and checked in the then-current `.workflow/` and copied Maestro documentation as a historical snapshot. The repository's default knowledge gate is now Graphify, not a repo-bundled Maestro search setup (ADR-032).

The same day's local-LLM implementation was committed the next afternoon as `a17303d`, but the work and live validation belong to the Aug 18 session. `orchestrator/adapters/local_llm.py`, `local_llm_bridge.py`, and the `codex.py` provider extensions added a separate `direct:local_llm` backend that preserves the Codex coding-agent harness while bridging a Chat Completions-only local server into the minimal Responses contract Codex expects. The bridge also had to consolidate system/developer instructions at the start of the transcript and compact the harness envelope enough for the observed 4,096-token upstream limit. By the end of that session the focused bridge tests, live LAN probes, and the full suite all passed (`Bugs.md` #44 resolved, ADR-033, `Research.md` topic 40).

---

### 2026-08-17 â€” Copilot adapter added and run memory moved into the target repository

Commit `d71c20e` added `orchestrator/adapters/copilot.py`, automatic backend registration, `COPILOT_BIN`, preflight recognition, generalized planner prompts, and foundation coverage. The adapter is non-raising and preserves stderr-only Copilot authentication/service failures, but the live smoke attempt was blocked by GitHub 503 responses, so production entitlement is not yet proven (`Bugs.md` #42, `Decisions.md` ADR-030).

Commit `1fa0c73` moved shared run memory into the target repository's `runs/` directory while retaining per-run plans, tasks, reviews, and events. It added legacy migration, first-run artifact initialization, precise local excludes, append-only/idempotent choices, locked cross-run learnings, and target-repository path propagation across the flow. The suite now collects 221 tests; current static validation passes, while this Windows checkout's full pytest attempt is blocked by temporary-directory ACL errors. A direct local OpenAI-compatible adapter remains planned separately from the Ollama-via-Codex bridge (`Bugs.md` #43â€“#44, `Decisions.md` ADR-029 and ADR-031).

---

### 2026-07-31 â€” Target architecture documented

The initial documents defined cross-agent routing, canonical context, resumable vendor sessions, worktree isolation, deterministic checks, and auditability before production code existed.

---

### 2026-08-03 â€” Maestro selected and handoff mechanics specified

Maestro became the external-agent delegation layer; durable entry-time handoff reconstruction and a configured budget ledger became the intended continuity mechanisms.

---

### 2026-08-04 â€” Preflight and learning sandbox added

The first production-track file was `orchestrator/preflight.py`. A separate LangGraph tutorial track was opened to learn routing and supervision without coupling experiments to the real system.

---

### 2026-08-05 â€” Tutorial graph executed end to end

Running the sandbox exposed routing, termination, transport, logging, and artifact-verification failures that later became explicit production constraints.

---

### 2026-08-06 â€” Real CLI adapters proven in the sandbox

Claude Code and Codex were invoked directly and concurrently. The experiments established stdin prompt delivery, explicit final-answer channels, wall-clock deadlines, non-raising adapter boundaries, and filesystem evidence over self-report.

---

### 2026-08-07 â€” Six-stage production orchestrator implemented

The design was researched with ChatGPT and Claude, specified in temporary pipeline notes, and implemented as the complete `orchestrator/` runtime: requirements, planner, wave orchestrator, merger, reviewer, supervisor, adapters, Git worktrees, artifacts, semantic routing, checkpoint/resume support, bounds, and audit logging.

The implementation commit is recorded by Git as `Workflow implemented` with **8,888 insertions across 53 files**; its timestamp crossed midnight locally, while the work and chat history belong to the August 7 session. The project added `requirements.txt`, `requirements-dev.txt`, `pytest.ini`, 10 test files, and **189 passing tests**. The temporary architecture notes were retired after their decisions and rationale were incorporated into the permanent documentation.

---

### 2026-08-08 â€” Windows-adapter regressions and vendor session resume fixed; two live runs diagnosed

Two Windows-specific defects in the adapter layer were fixed: Maestro binary resolution in `preflight.py` finally adopted the precedence `adapters/maestro.py` already used, and a new scrubbed subprocess environment (`adapters/base.py::subprocess_env()`) closed a `_bz2` import failure traced to inherited Python environment variables crossing a Windows console-script launcher boundary. Vendor session resume, previously non-functional on both Codex (a session id was captured but the session itself was discarded via `--ephemeral`) and Claude Code (`--no-session-persistence` returned a session id `--resume` then rejected outright), was proven broken empirically on both and fixed on both, so the reviewer's rework loop can now actually resume the agent that made a mistake rather than cold-starting or failing. Two real runs against external target repositories were separately diagnosed read-only, surfacing an unborn-repository worktree-setup failure and a browser-visible HTML defect the reviewer's checks did not cover. The test suite grew from 189 to 193 tests.

---

### 2026-08-09 â€” Dual coding slots added; Windows CLI deadlines made process-tree safe; parallel-run gaps recorded

Commit `4a123eb` (`Implement double coding subagents`) added two independently configurable coding slots and stable A/B assignment by task index, rebalanced five of six orchestration roles from Codex back to Claude while leaving planning on Codex, and moved all three vendor adapters onto the shared `run_with_deadline()` helper. On Windows, the helper creates a new process group and uses `taskkill /F /T /PID` after a timeout so the npm `.cmd` wrapper and its `node.exe` descendants release inherited pipes; this closes the hang that left requirements runs permanently silent after their nominal 300-second deadline (`Bugs.md` #34). The full suite grew to 197 passing tests. A nine-task parallel stress run separately confirmed two open design gaps: a rejected attempt resets the whole wave's integrated work, and task worktrees cannot directly access the canonical run artifacts, which are pasted into prompts and remain run-scoped (`Bugs.md` #35â€“#36). Commit `28b3aa4` then stored two temporary Bhai Digital Studio prompts, including a MongoDB/Express parallel-dispatch stress case, for repeatable manual runs.

---

### 2026-08-10 - Planner-sized rosters, path-reference prompts, and direct run-local learnings

Commit `436fb91` (`Enhance coding agent management and prompts in the orchestrator`) let the planner request a run-specific coding-agent roster, validated by `normalise_coding_agents()` against configured small/medium and expert model menus and capped at five agents. The wave orchestrator now dispatches against that roster with a run-wide round-robin offset, falling back to the old A/B coding slots only when no valid roster is supplied.

The same change replaced full artifact injection with file-path references across planner, coding, merger, reviewer, and supervisor prompts. Direct Claude/Codex adapters gained `extra_dirs` / `--add-dir` so agents can read the run directory from isolated worktrees, and `artifacts.py` gained an OS-locked `append-learning` CLI so coding agents can write `learnings.md` directly while other agents read it. The test suite remained at 197 tests and now covers roster validation plus real concurrent learning appends.

Live runs confirmed both the success path and remaining boundaries. `run-20260810-162135` completed and was accepted after reviewer rework, proving three Haiku coding agents could read artifacts by path and append learnings directly. `run-20260810-164949` exposed a stale `sonnet-5` model alias in the menu, fixed by switching to live aliases. `run-20260810-171145` then exposed the separate unsolved problem of live quota exhaustion: a valid `sonnet` agent can still be unavailable at dispatch time, so the budget/cooldown router remains future work.

---

### 2026-08-11 â€” Ollama adapter added as a zero-cost fallback; production evidence that the limit is model capability, not the pipeline

Hitting Claude's weekly rate limit at the very start of the session (`run-20260811-144518`, `-145041`) motivated `orchestrator/adapters/ollama.py`: bare `ollama run` has no file/shell/sandbox/session tooling, so it routes every `backend="ollama"` dispatch through Codex's existing agent-loop machinery via `run_codex(local_provider="ollama")`, with `-c model_reasoning_effort=none` because Codex otherwise assumes reasoning-capable models and Ollama's non-thinking models reject that mode. A same-day config option letting this routing choose Claude Code's harness instead of Codex's was proven broken in production within hours (`run-20260811-174803`: Claude Code's `--model` flag cannot accept an arbitrary local model tag) and removed outright rather than repaired â€” Codex is now the only Ollama harness (ADR-028, `Bugs.md` #39).

The adapter itself works: two runs completed the requirements stage cleanly through Ollama-via-Codex, at 142â€“347s depending on model size, with cosmetic (non-blocking) Codex/Ollama model-metadata warnings on every call. It does not yet make locally-hosted models reliable coding agents. Given real multi-turn coding-task dispatch rather than single-shot JSON generation, both Devstral and Qwen 2.5 Coder reported false task completion with zero files changed, and one run's Qwen agent both refused tool use outright and left a fabricated success entry in the run's shared `learnings.md` â€” a new failure class, since it is the first case in this project of a false claim entering the shared cross-agent evidence channel rather than only a model's own turn (`Bugs.md` #40â€“#41, `Research.md` topics 32â€“33). None of this was accepted by the pipeline: the reviewer's evidence-over-claims discipline (ADR-019/ADR-020) caught every instance by comparing claims against Git-observed files and by running the code rather than reading the report. The session's own conclusion, recorded in the repository's `Ollama Coding Agent Working.md`, is that the ceiling is model scale on the available hardware rather than a defect in the orchestrator â€” a ~70B-class coding-finetuned model is the untested next step. The suite grew from 197 to **209 tests** (new Ollama-adapter tests added, three since-removed Claude-harness tests subtracted).

---

### 2026-08-17 - Copilot adapter added; `runs/` artifacts moved into the target repository

The August 17 chat record shows two architecture-level changes. First, the orchestrator gained a Copilot adapter in the same overall adapter shape as the existing backends. That work added `orchestrator/adapters/copilot.py`, registered Copilot in the adapter base, updated config and planner prompt wording, extended preflight logic, and added focused coverage in `orchestrator/tests/test_foundation.py`.

Second, shared run artifacts were moved from the controller/root repository into the target repository so multiple runs could reuse the same context, choices, and learnings. The chat records the move to `target_repo/runs/` for shared artifacts and then the broader target-repository layout for plans, tasks, reviews, and events. Artifact handling and downstream consumers were updated around that change, and the user-supplied summary notes that test coverage expanded across foundation, requirements, planner, reviewer, supervisor, merger, and wave orchestration.
