"""The pipeline's graph state.

One rule decides what belongs here, borrowed from the sibling RAG-work project:
*if this graph were checkpointed and resumed tomorrow, would this value represent
the progress of this specific run?* Yes means state. No means it is configuration
or an injected dependency and belongs in config.py.

So the goal, the artifact paths, the wave cursor, the verdicts and the counters
are state; deadlines, model names and bounds are not.

Two annotations do real work here:

  * `Annotated[list, add]` on `events` and `wave_results` makes them accumulate.
    The default is last-write-wins, which would silently discard everything but
    the final writer's entry the moment two things write in one super-step.
  * `NotRequired` on every field a node may not reach, so reading one that was
    never set raises nothing.

And one trap worth stating, because it is the quiet one: LangGraph **silently
discards** an update addressed to a key that is not in this schema. An unknown
edge target is a compile-time error; an unknown state key is not an error at all
(docs/Bugs.md #5a). Every key any node writes must therefore appear below.
"""

from __future__ import annotations

from operator import add
from typing import Annotated, Any, Literal, NotRequired, TypedDict


def upsert_wave_results(
    existing: list[dict[str, Any]] | None,
    incoming: list[dict[str, Any]] | dict[str, Any],
) -> list[dict[str, Any]]:
    """Reducer for `wave_results`: append a new attempt, update an existing one.

    Plain accumulation is wrong here and plain replacement is wrong too. The wave
    orchestrator writes a record when it dispatches an attempt, and the merger
    then has to mark that same record merged — two writers, one row. Appending
    would leave two copies of one attempt disagreeing about whether it merged;
    replacing would lose every earlier wave.

    So: keyed upsert on (wave, attempt). A rework is a new attempt and appends; a
    merge result folds into the attempt it belongs to.
    """
    records = list(existing or [])
    updates = incoming if isinstance(incoming, list) else [incoming]

    for update in updates:
        if not isinstance(update, dict):
            continue
        key = (update.get("wave"), update.get("attempt"))
        for index, current in enumerate(records):
            if (current.get("wave"), current.get("attempt")) == key:
                records[index] = {**current, **update}
                break
        else:
            records.append(update)
    return records


def latest_task_verdicts(
    wave_results: list[dict[str, Any]] | None, wave: int
) -> dict[str, dict[str, str]]:
    """The most recently recorded keep/rework call per task in one wave.

    Scans every attempt's record for `wave` in list order — attempts append in
    ascending order, per `upsert_wave_results` above — and folds each record's
    `task_verdicts` in, so a later attempt's call for a task wins and a task
    never yet reviewed is simply absent. Each value is `{"verdict": "keep" |
    "rework", "reason": str}`.

    Shared by the reviewer (to tell a task apart that was already accepted in
    an earlier round from one it still has to judge) and the wave orchestrator
    (to decide which tasks to redispatch on a rework and which to leave
    merged as-is).
    """
    verdicts: dict[str, dict[str, str]] = {}
    for record in wave_results or []:
        if record.get("wave") != wave:
            continue
        verdicts.update(record.get("task_verdicts") or {})
    return verdicts


# The two feedback edges from the drawing, named. A verdict is one of these and
# never free text, so a router can act on it without parsing prose.
ReviewVerdict = Literal["approved", "rework"]
SupervisorVerdict = Literal["accepted", "replan"]

# Why the run ended. Bugs.md #15 is a run that stopped because a bound fired and
# recorded it identically to stopping because the work was done — the audit trail
# then reported the second. These keep the two apart.
RunStatus = Literal[
    "running",
    "completed",  # supervisor accepted, or the last wave merged with gates off
    "bounded",  # a termination guard fired; work may be incomplete
    "failed",  # an agent or a git operation failed unrecoverably
]


class TaskRecord(TypedDict):
    """One TASK-*.json file, as the planner wrote it."""

    task_id: str
    title: str
    description: str
    files: NotRequired[list[str]]
    acceptance: NotRequired[str]
    depends_on: NotRequired[list[str]]
    wave: NotRequired[int]


class WaveResult(TypedDict):
    """What one dispatch of one wave produced.

    Kept per attempt rather than per wave: a wave sent back for rework produces a
    second record, so the history shows what changed between the two rather than
    only the outcome that stuck.
    """

    wave: int
    attempt: int
    task_ids: list[str]
    # Per task: {task_id, ok, branch, worktree, session_id, report, cost_usd}
    tasks: list[dict[str, Any]]
    merged: bool
    merge_conflicts: NotRequired[list[str]]
    review_verdict: NotRequired[str]
    review_comments: NotRequired[str]
    # Per task: {task_id: {"verdict": "keep" | "rework", "reason": str}}. Only
    # tasks this attempt actually reviewed appear here — see
    # `latest_task_verdicts` for how a task's call from an earlier attempt of
    # the same wave is recovered.
    task_verdicts: NotRequired[dict[str, dict[str, str]]]


class PipelineState(TypedDict):
    """State carried along every edge of the graph."""

    # ── Run identity, set once by main.py ────────────────────────────────────
    run_id: str
    goal: str
    target_repo: str  # absolute path to the repository being worked on
    run_dir: str  # absolute path to this run's artifact directory

    # ── Agent 1: requirements gathering ──────────────────────────────────────
    # `context.md` is the requirements themselves. `user_choices.md` is the
    # stricter record: explicit user decisions only, no assumptions and no
    # inferences (docs/Architecture.md, "Artifact and Repository Boundaries").
    context_path: NotRequired[str]
    user_choices_path: NotRequired[str]
    context: NotRequired[str]  # inlined, so downstream agents need no file read

    # Handed from agent 1's survey node to its clarify node across the pause.
    # They exist because a node that calls interrupt() is re-executed from its
    # first line on resume and its updates were never applied — so anything
    # expensive must finish, and be committed to state, BEFORE the pause.
    survey_payload: NotRequired[dict[str, Any]]
    survey_questions: NotRequired[list[str]]
    survey_choices: NotRequired[list[str]]
    survey_session: NotRequired[str]

    # ── Agent 2: planner ─────────────────────────────────────────────────────
    plan_path: NotRequired[str]
    tasks: NotRequired[list[TaskRecord]]
    # Task ids grouped into dependency-ordered waves. waves[i] runs only after
    # waves[i-1] has merged.
    waves: NotRequired[list[list[str]]]
    # Which wave the pipeline is on. The inner Orchestrate -> Merge -> Review
    # loop advances this; the drawing shows the loop as a single pass.
    current_wave: NotRequired[int]
    # The coding-agent roster the planner sized for this plan: 1..
    # config.MAX_CODING_AGENT_COUNT entries, each {"backend", "model"}. A wave
    # of more than one task round-robins across these. Absent or empty falls
    # back to config.CODING_AGENT_A/B, which is what a pre-existing checkpoint
    # resumes into.
    coding_agents: NotRequired[list[dict[str, str]]]

    # ── Agent 3: wave orchestrator ───────────────────────────────────────────
    # Live worktrees for the wave in flight, so a crash leaves something to
    # reconcile against actual git state rather than a guess.
    active_worktrees: NotRequired[list[dict[str, Any]]]
    wave_results: Annotated[list[WaveResult], upsert_wave_results]
    # The integration branch's commit before this wave merged anything. A rework
    # resets the branch back to it, because the reviewer runs after the merger
    # and a rejected wave is therefore already integrated by the time it is
    # rejected. Cleared when the pipeline advances to the next wave.
    wave_base_sha: NotRequired[str]
    # Where the integration branch started, before any wave ran. A replan resets
    # to this: the supervisor rejected the whole result, the new plan is a fresh
    # decomposition of the same goal, and running it on top of the implementation
    # it replaces would double-implement whatever the two have in common.
    run_base_sha: NotRequired[str]

    # ── Agent 4: merger ──────────────────────────────────────────────────────
    merge_ok: NotRequired[bool]
    merge_report: NotRequired[str]
    integration_branch: NotRequired[str]

    # ── Agent 5: reviewer  (skipped entirely when ENABLE_REVIEWER is False) ──
    # None means "no verdict outstanding". The wave orchestrator clears it after
    # acting on a rework, so the next wave is not mistaken for a rejected one.
    review_verdict: NotRequired[ReviewVerdict | None]
    review_comments: NotRequired[str]
    # Rework rounds spent on the *current* wave, reset when a wave is accepted.
    rework_count: NotRequired[int]

    # ── Agent 6: supervisor  (skipped when ENABLE_SUPERVISOR is False) ───────
    supervisor_verdict: NotRequired[SupervisorVerdict]
    supervisor_comments: NotRequired[str]
    replan_count: NotRequired[int]

    # ── Audit ────────────────────────────────────────────────────────────────
    # Appended by every node. This is the run's own record of what happened and
    # why, and it is the thing that makes a finished run readable afterwards.
    events: Annotated[list[dict[str, Any]], add]
    status: NotRequired[RunStatus]
    # Why the run reached its terminal state, in one sentence. Populated for
    # "bounded" and "failed" as well as "completed", so the two are never
    # confused by whoever reads the log.
    stop_reason: NotRequired[str]
    total_cost_usd: NotRequired[float]


def initial_state(
    *,
    run_id: str,
    goal: str,
    target_repo: str,
    run_dir: str,
) -> PipelineState:
    """A fresh run's state, with every counter explicitly zeroed.

    Counters start here rather than defaulting inside the nodes that read them,
    so a bound can never be evaluated against a missing value.
    """
    return PipelineState(
        run_id=run_id,
        goal=goal,
        target_repo=target_repo,
        run_dir=run_dir,
        current_wave=0,
        rework_count=0,
        replan_count=0,
        wave_results=[],
        events=[],
        status="running",
        total_cost_usd=0.0,
    )


def event(kind: str, **fields: Any) -> dict[str, Any]:
    """One audit-log entry.

    Deliberately a plain dict rather than a class: it is written to state, and
    state is serialized by the checkpointer.
    """
    from datetime import datetime, timezone

    return {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, **fields}
