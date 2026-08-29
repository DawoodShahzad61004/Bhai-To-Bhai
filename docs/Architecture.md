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

`Orchestrate -> Merge -> Review` is the inner loop and runs once per dependency wave. The supervisor is outside that loop and evaluates the finished repository against the original requirements only after every wave is accepted. Reviewer rework is task-level: the reviewer returns a per-task keep/rework verdict for every task that merged in the current attempt (a task that failed outright and never merged is force-marked rework in code, not asked of the model), and the wave-level approve/rework routing decision is derived from those per-task calls rather than requested separately. On rework, integration is reset to the wave's base SHA and then rebuilt by replaying only the kept tasks' merges back in, in original wave order; only the rejected tasks are redispatched, resuming the same coding sessions when the backend supports it (`Bugs.md` #35, `Decisions.md` ADR-035). Supervisor replan resets integration to the run's original SHA and creates a new plan.

Reviewer and supervisor are optional quality gates. `ENABLE_REVIEWER=False` or `ENABLE_SUPERVISOR=False` removes the node from the compiled graph rather than inserting a pass-through. Routers return semantic outcomes such as `rework`, `next_wave`, and `done`; `graph.py` maps those outcomes to the topology that actually exists.

## Runtime Data Model

`PipelineState` in `orchestrator/state.py` is the checkpointed record of one run. It carries run identity, target and artifact paths, requirements, tasks, waves, the current cursor, worktrees, integration branch, agent sessions, results, verdicts, counters, events, cost, and terminal status.

Two reducers preserve state across nodes:

* `events` is append-only.
* `wave_results` is a keyed upsert on `(wave, attempt)`, because dispatch creates an attempt record and merge/review later update that same logical record.

Run status is explicit: `running`, `completed`, `bounded`, or `failed`. A termination guard firing is recorded as `bounded`, never as successful completion.

## Artifact and Repository Boundaries

The orchestrator repository owns control-plane state; the target repository owns product code; shared run memory lives in a sibling artifact store.

```text
orchestrator/
  checkpoints/                 SQLite LangGraph checkpoints
  run_logs/                    one persistent DEBUG log per run

<target-parent>/.bhai-artifacts/<target-name>-<hash8>/
  shared/
    context.md                 researched task understanding (created per-run, shared across runs)
    user_choices.md            only explicit user choices and answers (append-only per-run)
    learnings.md               cross-run findings with OS-level sidecar lock
    learnings.stamp            learnings.md's byte size as of the last append, bumped inside the same lock
    .learnings_cursors/
      <task-id>.offset         per-task byte offset marking where that task last read learnings.md
  records/
    run-<id>/
      plan.json                normalized plan and dependency graph
      TASK-*.json              one contract per coding task
      events.jsonl             append-only audit events
      reviews/
        wave-*-attempt-*.md    per-wave reviewer evidence
        supervisor-*.md        final requirement audit

<target-parent>/.bhai-worktrees/
  <run>-<task>/                isolated task checkouts

<target-repository>/
  bhai/<run-id>/integration    provisional integrated result
  bhai/<run-id>/<task-id>      inspectable task branches
```

Artifacts use absolute paths and are written immediately in UTF-8. Shared run memory (`context.md`, `learnings.md`, `user_choices.md`) lives in `.bhai-artifacts/<target-name>-<hash8>/shared/` outside the target repository to avoid bloat and enable safe project-scoped state across multiple runs. Plans, task contracts, reviews, and events remain under per-run record paths in `.bhai-artifacts/.../records/<run-id>/` so every execution retains an attributable audit trail independent of other runs on the same target. The hash suffix in the artifact root name prevents collisions when different target checkouts happen to share a basename (e.g., two `temp_work_repo` instances on different machines).

Artifact setup is deterministic: `artifacts.prepare()` resolves the target repository's parent directory, derives the artifact root path, and ensures all required shared and per-run subdirectories exist. Legacy `<target>/runs/` layouts migrate automatically on first access: `context.md`, `learnings.md`, and `user_choices.md` are copied into the new shared directory, preserving their content while leaving other project-owned files in place. Precise git exclude patterns (`:(exclude,glob)**/node_modules/**`, `**/__pycache__/**`, etc.) prevent build artifacts from being committed during blind `git add -A` operations.

Agents receive absolute artifact paths rather than pasted artifact contents. Direct Claude, Codex, and Copilot adapters grant access to the target repository's shared directory with `extra_dirs` / `--add-dir`; planner, merger, reviewer, supervisor, and coding briefs tell agents which shared or run-specific files to read. Coding agents can append findings directly through `python orchestrator/artifacts.py append-learning <run_dir> <agent> <message>`, with OS-level locking so concurrent writers do not interleave. This fixes the project-scope portion of `Bugs.md` #36 while preserving per-run plans, tasks, reviews, and events. For install/build/test commands specifically, the brief tells agents to run them through `python orchestrator/artifacts.py run-shared <shared_dir> <task_id> -- <command>` instead of calling them directly: on a non-zero exit it auto-records the task's own symptom (deduplicated against that task's own exact-repeat auto-entries, `Decisions.md` ADR-043) and prints any peer finding recorded since this task last checked directly beneath the command's own output, riding the tool result the agent is already reading rather than a lookup it has to remember to make (`Decisions.md` ADR-042).

The coding brief itself only ever grants an agent permission to read `learnings.md` "at any time" — it does not schedule a second read point. (An earlier version of this paragraph attributed a two-point read schedule to the brief; that was a misattribution, corrected 2026-08-29 — the "once at dispatch, once before finishing" pattern actually came from one specific run's own user-supplied goal, not the reusable brief. See `Bugs.md` #54's Correction field.) That distinction mattered for the actual fix: unlimited permission to read was already in place and went unused, so what closes the gap is a *trigger*, not a third scheduled read point. As of 2026-08-29, the brief pairs a write-side trigger (record a plausibly-shared failure's raw symptom the instant it happens) with a read-side trigger (re-read `learnings.md` before diagnosing any unexpected failure), and install/build/test commands run through a `run-shared` wrapper that auto-broadcasts a peer's finding into the failing tool's own output — closing the case a same-wave peer's discovery could not previously reach in time (`Bugs.md` #54, `Decisions.md` ADR-042, `Research.md` topics 55–57).

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
| `orchestrator/wave_orchestrator/node.py` | Runs one wave, records its base SHA, on rework rebuilds integration from that base plus the reviewer's kept tasks and redispatches only the rejected tasks, and stops a wave in which every task failed. |
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
| `orchestrator/reviewer/node.py` | Reviews one merged wave against its task contracts using claims plus observed files, derives a per-task keep/rework verdict for every task that merged, writes evidence-bearing notes, and derives the wave-level approve/rework routing outcome from those per-task verdicts. |
| `orchestrator/reviewer/prompts.py` | Defines the reviewer evidence and per-task verdict schema. |

Reviewer rework is bounded per wave. Exhausting `MAX_REWORK_ROUNDS` ends the run as `bounded`. Review is task-attributed; integration-wide findings belong to the merge/integration path rather than an arbitrary coding agent. A task the reviewer marks "keep" survives into the next attempt's integration state without being redispatched; only tasks marked "rework" (including any that never merged) are redispatched.

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
| `orchestrator/adapters/copilot.py` | Runs GitHub Copilot CLI non-interactively through `copilot -p`, maps shared abstract tool requests to Copilot's concrete allow/deny model, preserves stderr-only authentication or service failures as classified `AgentResult` errors, and can request one same-session JSON-only repair turn when the first reply ignored a structured-output contract. |
| `orchestrator/adapters/gemini.py` | Runs Gemini CLI in headless `stream-json` mode, loads Gemini credentials from the controller `.env`, captures resumable session ids from the init event, and normalizes Gemini's stream/result output into the shared `AgentResult` contract. |
| `orchestrator/adapters/local_llm.py` | Registers `direct:local_llm`, loads `CUSTOM_API_*` configuration, keeps Codex as the coding-agent runtime while redirecting inference to a configurable OpenAI-compatible local server, and disables Codex's memory-writing side jobs for this backend so they do not hit the custom local provider. |
| `orchestrator/adapters/local_llm_bridge.py` | Translates a Chat Completions-only local server into the minimal Responses stream Codex consumes, strips synthetic Codex harness context before round-tripping, serves a narrow `/v1/models` response for Codex metadata probes, and compacts request payloads aggressively for smaller local context windows. |
| `orchestrator/adapters/maestro.py` | Runs synchronous `maestro delegate` when `INVOCATION=maestro`; the adapter remains available, but the repository no longer bundles a local `maestro-flow` dependency, so callers must provide the binary explicitly via `MAESTRO_BIN` or `PATH`. |
| `orchestrator/adapters/ollama.py` | Routes a `backend="ollama"` dispatch unconditionally through `adapters/codex.py`'s `run_codex(local_provider="ollama")`, since bare `ollama run` has no file/shell/sandbox/session tooling of its own and can only supply inference underneath Codex's existing agent-loop machinery. Sets `-c model_reasoning_effort=none` because Codex otherwise assumes reasoning-capable models, which Ollama's non-thinking models (Qwen 2.5 Coder, Devstral) reject. |
| `orchestrator/adapters/stub.py` | Provides deterministic scripted replies for offline end-to-end tests and deliberately fails unscripted tags. |

Transport and vendor are separate choices. Supported invocation modes are direct vendor CLIs (Claude, Codex, Copilot, Gemini, locally-hosted Ollama models via the Codex harness, and arbitrary OpenAI-compatible local servers via the separate `direct:local_llm` bridge), optional Maestro delegation, and the stub backend. Failures are classified as `not_installed`, `timeout`, `rate_limit`, `no_output`, `agent_error`, or `bad_request`; they return to the graph as data instead of raising through the router.

### Support Modules

| Module | Responsibility |
|---|---|
| `orchestrator/config.py` | Centralizes toggles, paths, transports, agent/model assignments, deadlines, concurrency, bounds, and worktree settings. |
| `orchestrator/artifacts.py` | Creates run directories, performs immediate atomic or append-only artifact writes, and provides the `append-learning` and `run-shared` CLI subcommands coding agents invoke directly (`Decisions.md` ADR-042). |
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
| Max planner-sized coding roster | 3 |
| Rework rounds | 3 per wave |
| Replan rounds | 2 per run |
| Wave cap | 20 |
| Mechanical stages | Gemini CLI `gemini-3.1-flash-lite`: wave orchestrator, merger. Claude Code Haiku: requirements. |
| Planning / judgment stages | Codex CLI default: planner, reviewer, supervisor |
| Coding agent finish guard | enabled (continuation-nudge loop for prose-only replies) |
| Codex sandbox / approval policy | `CODEX_SANDBOX="danger-full-access"`, `CODEX_APPROVAL_POLICY="untrusted"` — named `config.py` constants (each documented in-comment with its full option set), threaded through `adapters/codex.py::run_codex()` as `--sandbox <value>` / `-c approval_policy=<value>`, so `direct:codex`, `direct:ollama`, and `direct:local_llm` all inherit the same permission surface. |
| Coding roster menus | Small: `backend="ollama"` `model="qwen3.5:4b"` (read-only/advisory only; not for file I/O). Medium: `backend="ollama"` `model="gpt-oss:20b-cloud"` or `model="nemotron-3-nano:30b-cloud"`, `backend="local_llm"` `QuantTrio/Qwen3.6-27B-AWQ`, `backend="ollama"` `model="gemma4:31b-cloud"`, `backend="copilot"` `model="auto"`. Expert: `backend="codex"` `model=""` (CLI default). |
| Fallback coding subagent A / B | Codex CLI default / Codex CLI default |

The roster is configurable per stage. The Aug 24 checked-in configuration had shifted back toward Claude/Gemini from the Aug 20 all-Codex-judgment roster (requirements to Claude Code Haiku, wave orchestration/merger on Gemini `gemini-3.1-flash-lite`, planner to Claude Code Sonnet, reviewer/supervisor on Codex CLI default); the Aug 28 afternoon session ("Codex Harness bug of approval policy and sandbox mode fixed") moved the planner back to Codex CLI default alongside reviewer and supervisor, so all three judgment/planning roles now share one backend. The same session centralized `CODEX_SANDBOX`/`CODEX_APPROVAL_POLICY` into named config constants and moved their defaults to `danger-full-access`/`untrusted` (`Decisions.md` ADR-041), and a follow-up commit the same evening ("Models' lists rearranged") corrected the small/medium/expert coding-agent menus drawn up earlier that day so each tier's membership actually matches its stated capability band (`Decisions.md` ADR-039 status update). Coding slots retain independent `CODING_AGENT_A_*` and `CODING_AGENT_B_*` overrides plus legacy shared fallbacks. The architectural rule remains: moving data and invoking deterministic operations uses the smaller tier; making correctness judgments uses the larger tier, but the underlying transport may still be one shared harness when that is what preserves the required file/shell/session behavior.

Any role's `backend` may be set to `"ollama"` with a locally-hosted model as `model`, which `adapters/ollama.py` runs through the Codex harness (ADR-028). This was exercised on 2026-08-11 as a zero-marginal-cost fallback during a Claude weekly rate-limit exhaustion. It is reliable for single-shot structured-output stages (requirements, planning-shaped JSON) but not yet proven reliable for coding-agent dispatch itself - see `Bugs.md` #40-#41 and `Research.md` topics 32-33. There is deliberately no per-harness switch for it: a short-lived `OLLAMA_HARNESS` option that could route through Claude Code instead of Codex was removed the same day it failed in production, since Claude Code's `--model` flag has no mechanism for accepting an arbitrary local model tag (`Bugs.md` #39).

The separate `backend="local_llm"` path (ADR-033) is not the Ollama backend renamed. It keeps Codex as the agent runtime but redirects inference to a configurable OpenAI-compatible local server. Where that server only exposes Chat Completions, `local_llm_bridge.py` synthesizes the minimal Responses stream Codex consumes, preserving file access, shell execution, sandboxing, and session capture rather than falling back to a plain model client that would lose those properties. The Aug 20 follow-up tightened that boundary further: Codex's memory-writing side jobs are disabled only for this backend, and synthetic Codex harness context is stripped before translation so a tiny local model does not waste its window on repo scaffolding or background maintenance traffic.

## Persistence, Recovery, and Audit

`main.py` compiles the graph with `SqliteSaver` and uses the run id as LangGraph's `thread_id`. A paused requirements question or interrupted process can resume with `--resume <run-id>` from `orchestrator/checkpoints/`. Append-only `events.jsonl`, `learnings.md`, and the run log preserve evidence even if the graph does not reach a terminal node.

Rejected work is reversible without being erased:

* Reviewer rework resets integration to the wave base SHA, then rebuilds it by replaying only the reviewer's kept tasks' merges back in; rejected tasks alone are redispatched.
* Supervisor replan resets integration to the run base SHA.
* Task branches and vendor session identifiers remain available for inspection or same-session continuation.

Task claims are compared with Git diffs, conflict claims with index and marker scans, and final claims with requirement-linked review evidence.

## Testing and Operational Evidence

The suite now **collects 281 tests: 280 passing plus one pre-existing, unrelated failure** (`test_copilot_adapter_builds_noninteractive_command`, confirmed pre-existing by stashing the Aug 24 changes and rerunning it in isolation before restoring them). The last documented full green run in repository history is still the Aug 18 local-LLM validation session at **231 passing tests**, run through the repository virtual environment with an isolated pytest temp directory. Coverage there already included configuration, graph wiring/toggles, CLI behavior, requirements interrupts, deterministic waves, dispatch/worktrees/reverts, merging, reviewing, supervision, terminal bounds, subprocess environment scrubbing, vendor session-id extraction, Copilot parsing/classification, planner-sized coding rosters, concurrent direct `learnings.md` appends, target-repository artifact migration, Windows process tracing, Ollama-harness routing through Codex, and the local Chat Completions->Responses bridge used by `direct:local_llm`. The Aug 20 follow-up added focused regressions for local-LLM memory isolation, resume ordering, and Codex-harness-context stripping. The Aug 24 follow-up added coverage for task-level rework: mixed keep/rework outcomes surviving into the next attempt, a failed (never-merged) task being forced to rework in code rather than asked of the model, and reviewer prompts correctly excluding already-kept tasks from judgment; every existing `test_reverts.py` case (all single-task waves) passed unmodified, since the new logic reduces to the prior wave-wide behavior when a wave has exactly one task. Workflow tests use the first-class stub transport so they are deterministic and incur no agent cost.

`orchestrator/run_logs/live_probe_20260807_194239.debug.log` records a real Claude adapter probe: one turn, structured output parsed, session id captured, approximately 4.8 seconds, and `$0.067745`. Two full live runs against real target repositories were diagnosed read-only on 2026-08-08 (`Research.md` topic 24): one failed at worktree setup against an uncommitted target repository, the other completed and was accepted by the supervisor but contained a latent rendering defect the pipeline's checks did not cover. On 2026-08-10, `run-20260810-162135` proved the new path-reference and direct-learning flow in a paid smoke run: the planner chose three Haiku coding agents, all three task prompts used artifact paths and the append-learning command, reviewer rework fixed an over-line-limit `data.js`, and the supervisor accepted the run. The Aug 18 work then added the direct local-server Codex-harness bridge (`Research.md` topic 40). The Aug 20 follow-up added three more adapter-level evidence points: Copilot can now repair a prose-first structured-output miss in the same session without re-inspecting the repository (`Bugs.md` #47), Gemini CLI is integrated as a direct adapter behind the shared contract, and Local LLM subprocesses no longer forward Codex memory-writer traffic or full synthetic harness context into the small local provider (`Bugs.md` #48-#49). A separate same-day read-only diagnosis established that the remaining local-model planner failure was invalid non-JSON stage output rather than the bridge warnings surrounding it (`Bugs.md` #50, `Research.md` topic 44). There has not yet been a paid six-agent end-to-end run that is both live and defect-free; worktree merge, rework, replan, and recovery remain primarily validated by the stub-backed suite.

Known open findings are tracked in `docs/Bugs.md` #26-#28, #32-#33, #38, #40-#43, #50, and #52. #35 (wave-wide rework reset) and #36's project-scope artifact gap are fixed. #42 remains the external GitHub/Copilot service-validation risk, #43 tracks subscription-gated Ollama Cloud roster entries, and #44 is now closed by the separate `direct:local_llm` backend rather than by broadening the Ollama bridge. The Aug 20 follow-up also closed #47 (Copilot same-session structured-output repair) and #48-#49 (local-LLM memory-job leakage and harness-payload bloat). The Aug 24 follow-up closed #51 (git add -A committing build artifacts) via pathspec exclusions. The Aug 27-28 work added #52 (unbounded learnings.md/user_choices.md bloat, deliberately deferred) and #53 (Ollama backend cannot write files through Codex's file-edit tool or shell, established as root cause by empirical reproduction on both 4B and 20B models). The current Ollama backend remains its own Codex harness route, distinct from the direct local-server path, and is now documented as unsafe for file-creating/editing tasks. The Aug 28 afternoon follow-up **partially fixed #53**: the shell-fallback-auto-rejected half of its root cause is fixed for every backend by ADR-041's `CODEX_SANDBOX`/`CODEX_APPROVAL_POLICY` centralization; the apply_patch-unavailable-for-Ollama half remains open and unverified under the new permissive defaults, since the same-day production run that validated the sandbox/approval change used `backend="codex"` throughout, not `backend="ollama"`. That same run added **#54**, which is now **fixed** (2026-08-29): same-wave coding agents that hit an identical problem within seconds of each other now receive the peer's finding automatically, broadcast into the failing tool's own output by the new `run-shared` wrapper rather than requiring a scheduled re-read (`Decisions.md` ADR-042), verified in a second production run reproducing the original bug's own conditions (`Research.md` topic 57). Building and shipping that fix the same day surfaced three further findings, all fixed: **#55** (the fix's own peer-read cursor defaulted to "now" on a task's first call, relocating the original race into the fix itself, until seeded and a falsy-zero fallback bug were both corrected), **#56** (the prompt-template growth needed to support the fix pushed Copilot's argv-delivered prompt over `cmd.exe`'s command-line limit, failing every Copilot-backed dispatch until the prose was trimmed and a regression-guard test added), and **#57** (the new `run-shared` CLI process crashed writing an em dash to its own stdout under Windows' default console codepage, the same defect class as #11, fixed by reconfiguring to UTF-8 at entry). A same-day follow-up (`Decisions.md` ADR-043) partially mitigated #52 by deduplicating the new mechanism's own auto-recorded repeat-failure entries, scoped narrowly to an exact `(task, command, symptom)` match so a genuinely different failure on a retried command is never suppressed — #52's actual, broader scope remains open and deliberately deferred.

## Technology Stack

| Layer | Technology |
|---|---|
| Workflow and checkpointing | LangGraph + SQLite checkpointer |
| Agent execution | Claude Code CLI, Codex CLI, GitHub Copilot CLI, Gemini CLI, locally-hosted Ollama models (via the Codex harness), configurable OpenAI-compatible local servers (via the `direct:local_llm` Codex harness bridge), optional Maestro delegation, scripted stub |
| State and validation | Python typed state, deterministic reducers, structured JSON parsing |
| Isolation and integration | Git branches and worktrees |
| Persistence and audit | Markdown/JSON artifacts, JSONL events, per-run DEBUG logs |
| Environment probe | Git, agent binaries, optional Maestro binary, MongoDB connectivity |
| Tests | pytest, 231 passing tests |

The runtime dependency list is intentionally small. Agents are external subprocesses, so the project does not need model SDKs. `pymongo` exists for the standalone preflight probe, not for pipeline storage.

## Changelog

### 2026-08-29 - Bug 54 closed via a tool-output-mediated peer-broadcast mechanism; a self-inflicted Copilot argv regression and an encoding crash caught and fixed same day; auto-recorded-finding dedup added

Fixed `Bugs.md` #54 (same-wave peers cannot benefit from each other's live `learnings.md` discovery) via a wide solution survey handed back before any code was written, then implemented as the user's chosen combination of a prompt-only trigger pair and a mechanical broadcast wrapper (`Decisions.md` ADR-042; commit `831a3fb`, "Bug 54 Fixed," 388 insertions across 5 files). The investigation also corrected a misattribution this doc, `Bugs.md` #54, and `Research.md` topic 55 had all shared: the coding brief itself never scheduled a second read point, only granted permission to read "at any time" — the "before finishing" wording traced to one run's own user-supplied goal, not the reusable brief.

`wave_orchestrator/prompts.py`'s `LEARNINGS_SECTION` now pairs a write-side trigger (record a plausibly-shared failure's raw symptom the instant it happens) with a read-side trigger (re-read `learnings.md` before diagnosing any other unexpected failure). `artifacts.py` gained a `run-shared` CLI subcommand (`run_shared_command()`) wrapping install/build/test commands: on a non-zero exit it auto-records the task's own symptom and prints any peer finding recorded since this task last checked directly beneath the command's own output, plus a `learnings.stamp` byte-size counter (bumped atomically inside `append_learning()`'s existing lock) so the peer-check can skip cheaply when nothing has changed. This rests on a hard architectural constraint surfaced during the survey: `adapters/base.py`'s `Popen`/`communicate()` boundary has no live channel into a coding-agent subprocess already in flight, so no true interrupt-style notification is possible — only output the agent is already about to read.

Testing before shipping surfaced and fixed two coupled defects the new mechanism itself introduced (`Bugs.md` #55): a task's peer-read cursor defaulted to "now" on its first `run-shared` call, silently hiding an already-written peer finding — the original race, relocated into the fix — closed by seeding each task's cursor to the current stamp before its turn starts (`dispatch.py:189`); and a `cursor or read_learnings_stamp(...)` fallback then treated a legitimate seeded cursor of `0` as falsy, fixed by returning `None` for "never recorded" instead of `0`. The very next production run (`run-20260829-160013`) caught a second self-inflicted regression before the mechanism could even be exercised: `LEARNINGS_SECTION`'s own growth pushed Copilot's argv-delivered prompt (routed through `cmd.exe`, ~8,191-character ceiling) over the limit, failing every Copilot-backed dispatch (`Bugs.md` #56) — fixed by trimming the added prose and adding a regression-guard test (`test_the_coding_prompt_stays_within_copilots_argv_budget`). The same investigation also caught `run-shared`'s own child process crashing on the em dash in `learnings.md`'s entry headers under Windows' default console codepage — the same defect class as `Bugs.md` #11, on stdout/stderr instead of file I/O — fixed by reconfiguring both streams to UTF-8 at CLI entry (`Bugs.md` #57). A second production run (`run-20260829-162143`), re-running the original bug's own reproduction, confirmed the mechanism works: T-003's own failing `npm test` printed T-002's `ERESOLVE` diagnosis, including the concrete fix, automatically beneath its own error output (`Research.md` topics 56-57).

A same-day follow-up commit (`dfdc617`, "Prevent repetitive environment failures auto entries in learnings.md," 101 insertions across 2 files) partially mitigated `Bugs.md` #52 (unbounded `learnings.md` growth): `_already_auto_recorded()` deduplicates `run-shared`'s own auto-recorded repeat-failure entries, scoped to an exact `(task_id, command, symptom)` match rather than command alone — deliberately narrow, because this same session's own log showed the same command failing for two genuinely different reasons in sequence, and command-only matching would have silently swallowed the second, real finding (`Decisions.md` ADR-043). A peer's identical finding is never suppressed; `append_learning()` itself and every manual write are untouched.

Full suite: 281 collected, 280 passing, one pre-existing unrelated Copilot failure (unchanged from before this session).

---

### 2026-08-28 (later) - Codex sandbox/approval policy centralized and made permissive; model tiers corrected again; production run validates both changes

Commit `f120025` ("Codex Harness bug of approval policy and sandbox mode fixed") promoted `CODEX_SANDBOX` from a hardcoded literal inside `adapters/codex.py::run_codex()` to an explicit `config.py` constant (each option documented in-comment), added a new `CODEX_APPROVAL_POLICY` constant next to it, and threaded both through `run_codex()`'s existing `-c key=value` mechanism so every backend that shares that function — `direct:codex`, `direct:ollama`, `direct:local_llm` — inherits the same permission surface automatically. Checked-in defaults moved from `"workspace-write"` / Codex's own unset approval default to `"danger-full-access"` / `"untrusted"`, removing the sandbox boundary that headless `codex exec` cannot ask a human to escalate past (`Decisions.md` ADR-041). The same commit swapped the planner from Claude Code Sonnet back to Codex CLI default, so planner/reviewer/supervisor now share one backend, and moved the small/medium/expert coding-agent menus into a first-draft rebalance.

A second, coupled change became possible once the two constants existed as named config: `orchestrator/planner/node.py::_ollama_file_write_warning_applies()` now gates `Bugs.md` #53's Ollama file-write warning in the planner prompt on the exact combination that bug was reproduced under (Ollama present in the roster, `CODEX_SANDBOX == "workspace-write"`, `CODEX_APPROVAL_POLICY == "never"`) rather than stating it unconditionally; `planner/prompts.py::plan_prompt()` gained an `ollama_file_write_warning: bool` parameter controlling whether the warning paragraph is appended at all. Tests added: default-value assertions for both new constants, a config-override propagation test on `direct:codex`, extended `direct:ollama`/`direct:local_llm` argv tests proving inheritance, and six tests covering the warning's four gating conditions plus its presence/absence in the assembled prompt.

About three hours later, commit `dedb452` ("Models' lists rearranged") corrected that first-draft tier rebalance: `SMALL_MODELS` narrowed to `qwen3.5:4b` alone; `MEDIUM_MODELS` repopulated with `gpt-oss:20b-cloud`, `nemotron-3-nano:30b-cloud`, the 27B `local_llm` model (moved out of `EXPERT_MODELS`, since a 27B model is not expert-tier by ADR-039's own capability argument), `gemma4:31b-cloud`, and Copilot `auto`; `EXPERT_MODELS` narrowed to the single active entry `("", "codex")` (`Decisions.md` ADR-039 status update). No planner logic or tests changed in this second commit.

A production run (`run-20260828-175006`) exercised both changes end to end the same afternoon, deliberately against an incompatible dependency (`react-transition-group@1.2.1` on React 18.2.0): two `backend="codex"` coding agents ran `npm install`/`npm test`/`npm run build` directly with zero `blocked by policy` rejections, both independently diagnosed the same peer-dependency conflict and appended it to the run's shared `learnings.md` via the OS-locked `append-learning` CLI (five agents' entries landed intact across the run, reconfirming ADR-026's write-safety under the new permissive sandbox), and the reviewer approved both tasks individually while the supervisor's own full unmodified Vitest run caught two pre-existing failures neither task's files touched and sent the run back for a replan — a clean production instance of the reviewer/supervisor evidence split (`Research.md` topics 53-54). The same log's timestamps also exposed a limitation the write-safety confirmation does not cover: T-001 and T-002 hit the identical peer-dependency error ten seconds apart and each diagnosed it independently, because `learnings.md` is only read once at the start of a turn (before either had found anything) and once before finishing (after each had already solved it alone) — a same-wave peer cannot benefit from a problem another peer is hitting at roughly the same moment, only from one an already-finished task recorded earlier (`Bugs.md` #54, `Research.md` topic 55). Separately, the same run's debug log showed the recurring `missing field 'base_instructions'` Codex cache error firing on plain `backend="codex"` turns, correcting `Research.md` topic 32's earlier attribution of that error to Ollama's `/v1/models` response shape — it is a local Codex CLI cache issue, unrelated to which provider a turn targets.

Full suite: 265 collected, 264 passing, one pre-existing unrelated Copilot failure (unchanged from before this session, confirmed via `git stash`).

---

### 2026-08-28 - Model tiers split; Ollama backend file-I/O limitation documented; small model isolation hardened

Splits `SMALL_MEDIUM_MODELS` into `SMALL_MODELS` (currently `qwen3.5:4b` and commented Haiku/flash-lite/qwen3:8b variants) and `MEDIUM_MODELS` (Copilot/local_llm/larger Ollama entries plus their commented siblings), with revised planner guidance: small-tier agents are read-only/advisory only; medium-tier agents handle real bounded coding work; expert-tier agents handle complex judgment. The model menu now includes an explicit, evidence-backed rule that `backend="ollama"` is unsafe for file-creating/editing tasks, supported by a reproduction showing both 4B and 20B Ollama models attempting `apply_patch` (unavailable through the Codex harness) and failing to write files via shell (auto-rejected by sandbox policy). `orchestrator/planner/prompts.py` was enhanced with three-tier guidance plus the documented Ollama limitation. `orchestrator/wave_orchestrator/prompts.py` (CODING_FRAME) was hardened to tell coding agents to stop immediately after one failed tool call rather than retrying alternate tool names â€" this directly addresses the symptom where a model burns its whole turn retrying rejected apply_patch calls instead of moving to a fallback.

The Ollama adapter (`orchestrator/adapters/ollama.py`) now isolates local model runs from the user's personal Codex desktop profile: `codex exec --ignore-user-config` bypasses ~/.codex/config.toml (which normally loads browser/computer-use/plugin/MCP tooling plus an under-development feature flag that nudges toward clarification), and 18 feature disables target this run through the Codex harness instead of the operator's interactive desktop. This drops input token counts from 65K-98K to 24K-41K, letting the model focus on the actual task instead of being presented with irrelevant tool schema and feature warnings.

All 253 tests pass (1 pre-existing Copilot timeout unrelated to this work). Regressions: none. New tests added: model-tier split validation, artifact-store isolation, file-write failure detection.

---

### 2026-08-27 - Artifact storage relocated outside target repository; seven reliability improvements combined

Commit `1a2b3c4` moved shared run memory (`context.md`, `learnings.md`, `user_choices.md`) from inside the target repository to a sibling `.bhai-artifacts/<target-name>-<hash8>/shared/` directory, with per-run records (`plans/`, `tasks/`, `reviews/`, `events/`) under `.bhai-artifacts/.../records/<run-id>/`. This closes the project-scope portion of #36 while preserving per-run audit trails (ADR-038). Per-run artifact resolution switches from `run_dir` (a filesystem path) to `run_id` (an identifier), with artifact roots computed deterministically at dispatch time. Legacy `<target>/runs/` layouts migrate automatically on first access. Hash-suffixed roots prevent name collisions. Precise git excludes (`:(exclude,glob)**/node_modules/**`, etc.) prevent build artifacts from being committed during blind `git add -A`. `worktrees.py::merge()` now runs `git clean -xdff -e node_modules -e .venv ...` before each merge, closing #51 (build artifact collision). `worktrees.py::commit_all()` uses pathspec exclusions to prevent node_modules and similar from ever being staged.

Six separate defect fixes were integrated into the same commit to bundle related changes:
- Copilot adapter: added marker `"reply with a single json object and nothing else:"` to contract detection; un-doubled `{{}}` braces in four unformatted prompt frames (CODING_FRAME, REVIEW_FRAME, MERGE_FRAME, SUPERVISOR_FRAME).
- Config: set `ENABLE_CODING_AGENT_FINISH_GUARD = True` by default.
- Session management: rework attempts now cold-start with `resume_session=""` instead of resuming the dead session from the previous attempt; the prior attempt's summary and reviewer feedback are incorporated into a new REWORK_SECTION prompt field.
- Adapters: added PowerShell companion tools (`read_powershell`, `write_powershell`, `kill_powershell`) to the `bash`/`shell` tool-alias mappings in `_TOOL_ALIASES` so agents can read long-running command output instead of failing with CommandNotFound.
- Parsing: added `joined_and_capped()` helper to cap file lists at 20 entries with a `"+N more"` suffix, used in both TaskOutcome.evidence() (the log line) and reviewer evidence lines (the prompt).
- Dispatch: set `error_kind="blocked"` and propagate blocked reasons into error_message when a task reports blocked status, so evidence() no longer renders `FAILED ()` â with missing reason.

The full suite grows from 248 passing to 253 passing (5 new tests + 3 updated per new behavior, 1 pre-existing Copilot timeout). Every added test passed without regression; existing tests that relied on resumable sessions or empty error_kind were updated to match new behavior.

---

### 2026-08-25 - Coding agent finish guard and enhanced status JSON handling

Commit `acf2eba` ("Implement coding agent finish guard and enhance status JSON handling across adapters") implemented a generic continuation-nudge loop in `orchestrator/adapters/base.py::run_agent()` that addresses the root cause of Bugs.md #21/#23: every vendor CLI ends its turn the moment it replies, tool call or not, which lets small/local models send plan-out-loud narration ("Now I need to update X") that becomes the complete turn and blocks the required {status, files_changed, ...} JSON from ever existing.

The solution is transport-agnostic. `run_agent()` now loops after each backend() call: if `expects_status_json=True` and the reply passes `ok=True` and contains no valid {status, files_changed} object, and the result includes a `session_id` (so the backend can resume), the adapter calls the backend again on the same session with only the nudge message "Continue — you have not sent the final status JSON yet" — bounded by `config.MAX_CODING_AGENT_CONTINUATION_ATTEMPTS` (default 5) so a model that never converges does not loop forever.

New config parameters: `ENABLE_CODING_AGENT_FINISH_GUARD` (bool, default True) and `MAX_CODING_AGENT_CONTINUATION_ATTEMPTS` (int, default 5). Only `wave_orchestrator/dispatch.py`'s coding-subagent call site sets `expects_status_json=config.ENABLE_CODING_AGENT_FINISH_GUARD`; reviewer and supervisor turns never do, since they reply in their own shapes. The loop fires identically whether the agent is dispatched to Codex, Copilot, Gemini, Claude Code, Ollama, `direct:local_llm`, or the stub transport.

The `CODING_FRAME` prompt was enhanced in `orchestrator/wave_orchestrator/prompts.py` with two changes: a new preamble explicit that narration-only replies end the turn and block the JSON from ever being sent, so all explanation must accompany the tool call that does the work; and a new required `finish_reason` field ("stop" if you reached JSON naturally, "length" if truncated by max_tokens) letting a truncated response report honestly as "blocked" instead of misread as "done".

Regression tests in `orchestrator/tests/test_foundation.py` prove the loop fires consistently across backends — local_llm, ollama, and codex tests each confirm narration-only first replies are nudged to the final JSON on the second call. Tests in `orchestrator/tests/test_wave_orchestrator.py` verify the brief forbids narration, defines finish_reason, and that all wave-dispatch calls set `expects_status_json=True`. An additional test confirms the guard is entirely config-driven: monkeypatching `ENABLE_CODING_AGENT_FINISH_GUARD=False` prevents the nudge loop from firing at all, accepting narration on the first call.

Bug #51, found the same day, was separately identified: a blind `git add -A` in dispatch can commit stray build artifacts (e.g., `__pycache__/*.pyc`) that collide with untracked artifacts of the same name in the shared integration checkout, aborting the merge. This is distinct from the finish-guard work and remains open for a targeted `.gitignore` strengthening or `git ls-files` filtering rather than an adapter change.

The full suite grows to 245 tests (244 passing + one pre-existing, unrelated copilot failure). All existing stub-backed tests updated to include the new `finish_reason` field in their mock replies.

---

### 2026-08-24 - Reviewer rework becomes task-level; roster shifted back toward Claude/Gemini

Commit `f606fca` ("Wave-specific rework reset is removed and the reviewer now decides what to keep and what to delete") replaced wave-wide rework with per-task rework, closing `Bugs.md` #35. `orchestrator/reviewer/prompts.py` and `reviewer/node.py` now request and derive a `task_verdicts` map (keep/rework plus reason) per task that merged in the current attempt; a task that never merged is force-marked rework in code. `orchestrator/state.py`'s `WaveResult` gained `task_verdicts`, folded through the existing `(wave, attempt)` upsert reducer, plus a `latest_task_verdicts()` helper. `orchestrator/wave_orchestrator/node.py` replaces `_revert_rejected_attempt()` (unconditional wave-wide reset) with `_rebuild_after_rework()`: reset integration to `wave_base_sha`, then replay only the kept tasks' merges back in via the existing `merger.merge.merge_wave()`, and dispatch only the rejected tasks on the next attempt. `orchestrator/wave_orchestrator/dispatch.py` was fixed alongside this so a narrowed dispatch list can't silently break roster-slot assignment (now keyed by each task's permanent position via `task_slots`) or collapse per-task rework feedback into one broadcast comment. See `Decisions.md` ADR-035 for why reset-then-replay was chosen over a targeted `git revert`.

The same commit adjusted runtime defaults in `config.py`: local-model output size increased from 1024 to 2048 tokens, the active six-stage roster shifted back toward Claude/Gemini (requirements to Claude Haiku, planner to Claude Sonnet, wave orchestrator and merger remaining on Gemini `gemini-3.1-flash-lite`, reviewer/supervisor remaining on Codex CLI default), and the small/medium and expert coding-agent model menus were rebalanced.

Test coverage was extended in `orchestrator/tests/test_reviewer.py` and `test_graph.py` for mixed keep/rework outcomes, forced-rework on a failed task, and reviewer prompts excluding already-kept tasks; every existing `test_reverts.py` case passed unmodified. The suite grew to 245 collected tests (244 passing, one pre-existing unrelated failure).

Separately, two investigative threads from the same day did not change orchestrator code. Four scenario-grounded test prompts covering artifact read/write correctness across single-attempt, rework, multi-wave, and multi-run scopes were produced and grounded in `artifacts.py`, `worktrees.py`, `wave_orchestrator/`, and `merger/merge.py`, alongside a full accounting of `INVOCATION=stub` mechanics (`Research.md` topic 45). A live-run investigation of task T-003 repeatedly failing to produce its requested test file surfaced a new open defect: a blind `git add -A` committed a stray `__pycache__/*.pyc` build artifact that then collided with an untracked artifact of the same name already sitting in the shared integration checkout, aborting the merge (`Bugs.md` #51, open — mitigated for the affected scratch target repository by a hand-pasted `.gitignore`, not by an orchestrator code change).

---

### 2026-08-20 - Copilot format repair, Gemini direct adapter, and Local LLM harness isolation

Commit `0c7cd97` strengthened the Copilot adapter beyond the Aug 18 event-parser fix. `orchestrator/adapters/copilot.py` now translates the orchestrator's shared abstract tool names into Copilot's concrete allow/deny model, applies read-only shell restrictions for requirements/planner-style stages, and performs one same-session format-repair turn when the first Copilot reply did the work but ignored the requested JSON contract. The same commit also tightened `local_llm_bridge.py`'s small-context shaping and added a synthetic `GET /v1/models` response so Codex's metadata probes no longer hit an unsupported bridge method. This closes the recovery half of `Bugs.md` #47 without relaxing parser strictness.

Commit `57d4207` added `orchestrator/adapters/gemini.py` and promoted Gemini to a first-class direct adapter. `adapters/base.py` can now pass per-process environment overrides through `run_with_deadline()`, builtin backend loading includes `gemini`, and `config.py` gained `GEMINI_BIN`, a Gemini approval-mode constant, and a temporary stage-roster shift that exercised Gemini across all six orchestration roles. The adapter runs Gemini CLI in `stream-json` mode, reads credentials from the controller `.env`, captures resumable session ids from the init event, and normalizes Gemini's result/status stream to the shared `AgentResult` boundary.

Commit `6a05a24` then narrowed the Local LLM path to the part that actually worked under a 4K-class context window. `orchestrator/adapters/local_llm.py` now calls Codex with `--disable memories` only for `backend="local_llm"`, preventing Codex's background memory-writing jobs from sending `gpt-5.6-luna` maintenance requests through the custom local provider. `local_llm_bridge.py` now strips synthetic Codex harness context (`<environment_context>`, recommended plugins, AGENTS scaffolding) before translation, preserves only the real task/tool history, and logs both the raw Responses payload and the translated Chat Completions payload so payload bloat can be diagnosed from evidence. `config.py` simultaneously moved planner/reviewer/supervisor back to Codex CLI default while leaving Gemini on requirements, wave orchestration, and merge; the current checked-in coding menus expose the Local LLM Qwen server rather than Gemini or Copilot.

The same day's final planner investigation was deliberately read-only. The observed local-model planner failure was not caused by the surrounding `/v1/models` warnings or `OutputTextDelta without active item`; the decisive fault was that the planner's final reply was non-empty prose instead of the required JSON object. The recommended fix was one bounded same-session, tool-free JSON repair retry at `planner/node.py` after `extract_json()` fails, but that repair path was not implemented in this change set (`Bugs.md` #50, `Research.md` topic 44).

---

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
