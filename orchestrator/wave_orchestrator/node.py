"""Agent 3 — wave orchestrator.

    Plan -> [Orchestrate] -> Merge
              ^
              └── rework, from the Reviewer

Reads the plan and runs one wave of coding subagents per invocation. The drawing
shows `Orchestrate -> Merge -> Review` as a single pass; it is actually the
pipeline's inner loop, executed once per wave, with the supervisor sitting
outside it (docs/Architecture.md, "High-Level Architecture").

This is the other re-entry point. The reviewer sends a wave back here when the
implementation is unacceptable — the plan was fine, the work was not — and the
notes are specific about what that means: the subagent's worktree is reverted,
and the same session is addressed again. Both happen here.

Smaller model, and it barely uses one: this box reads JSON and dispatches
processes. The judgment lives in the coding subagents it starts and in the
reviewer that follows it.
"""

from __future__ import annotations

import artifacts as art
import config
import worktrees as wt
from logging_config import get_logger
from merger.merge import merge_wave
from state import PipelineState, event, latest_task_verdicts
from wave_orchestrator.dispatch import run_wave

logger = get_logger(__name__)

AGENT = "wave_orchestrator"


def _sessions_from(state: PipelineState, wave: int) -> dict[str, str]:
    """Vendor sessions from this wave's previous attempt, by task.

    Read from the most recent attempt at this wave, so a rework addresses the
    agent that did the work rather than a fresh one that has to be told what it
    is looking at.
    """
    for record in reversed(state.get("wave_results") or []):
        if record.get("wave") != wave:
            continue
        return {
            task["task_id"]: task.get("session_id", "")
            for task in record.get("tasks", [])
            if task.get("session_id")
        }
    return {}


def _attempt_number(state: PipelineState, wave: int) -> int:
    return sum(1 for record in state.get("wave_results") or [] if record.get("wave") == wave)


def _coding_agents_from(state: PipelineState) -> list[config.AgentSpec]:
    """The roster the planner sized (state["coding_agents"]), or the default pair.

    A checkpoint from before dynamic sizing existed, or a plan that provided
    nothing usable (planner_node already validated and fell back at write
    time), has no roster in state — the same two-slot default this pipeline
    always ran with covers both.
    """
    entries = state.get("coding_agents") or []
    if not entries:
        return [config.CODING_AGENT_A, config.CODING_AGENT_B]
    deadline = config.CODING_AGENT_A.deadline_seconds
    return [
        config.AgentSpec(backend=entry["backend"], model=entry["model"], deadline_seconds=deadline)
        for entry in entries
    ]


def _task_outcome(state: PipelineState, wave: int, task_id: str) -> dict | None:
    """The most recently recorded outcome dict for one task in one wave.

    Supplies `merge_wave`'s task dicts during a rebuild replay (see
    `_rebuild_after_rework` below), so conflict-resolution context — the
    task's own `report` — is the real thing, not a bare synthetic stand-in.
    """
    outcome = None
    for record in state.get("wave_results") or []:
        if record.get("wave") != wave:
            continue
        for task in record.get("tasks") or []:
            if task.get("task_id") == task_id:
                outcome = task
    return outcome


def _rebuild_after_rework(
    state: PipelineState,
    branch: str,
    *,
    wave_index: int,
    kept_ids: list[str],
    artifacts,
) -> tuple[bool, str]:
    """Undo a rejected attempt at task granularity: keep what was accepted, discard the rest.

    Previously this reset the whole wave's integration result on any rework,
    including tasks that had already succeeded (docs/Bugs.md #35). Now only
    the rejected tasks' contribution is removed.

    Two halves. Every worktree from the just-finished attempt is destroyed —
    a kept task's worktree is done and nothing else will ever clean it up
    (wt.cleanup() is never called from production code); a rejected task's
    worktree is about to be recreated fresh anyway, which wt.create() already
    handles for a leftover path. Then the integration branch is reset all the
    way back to where this wave started (`wave_base_sha`) — because the
    reviewer runs *after* the merger, so a rejected attempt has already been
    integrated by the time anyone says part of it is wrong — and every
    currently-"keep" task's branch is re-merged into it, in the wave's
    original task order, by calling the same `merge_wave` the merger node
    itself uses. That reuse is deliberate: it is exactly what turns "keep task
    A, discard task B" into a real git operation without inventing a second
    conflict-resolution path.

    Each task's own branch survives this untouched either way, so a rejected
    attempt stays inspectable afterwards, same as before.
    """
    stale = state.get("active_worktrees") or []
    if stale:
        logger.info("[%s] discarding %d worktree(s) from the rejected attempt", AGENT, len(stale))
        for record in stale:
            path = record.get("worktree")
            if path:
                wt.remove(state["target_repo"], path)

    base_sha = state.get("wave_base_sha", "")
    if not base_sha:
        return True, ""
    reset = wt.reset_branch(state["target_repo"], branch, base_sha)
    if not reset.ok:
        message = f"could not reset {branch} to {base_sha[:8]}: {reset.stderr}"
        logger.warning("[%s] %s", AGENT, message)
        return False, message

    if not kept_ids:
        return True, ""

    kept_tasks = [_task_outcome(state, wave_index, task_id) for task_id in kept_ids]
    kept_tasks = [task for task in kept_tasks if task]
    logger.info(
        "[%s] rebuilding %s with %d kept task(s): %s",
        AGENT, branch, len(kept_tasks), ", ".join(kept_ids),
    )
    report = merge_wave(
        target_repo=state["target_repo"],
        into=branch,
        tasks=kept_tasks,
        context_path=str(artifacts.context),
        artifacts_dir=str(artifacts.shared_dir),
    )
    if not report.ok:
        message = f"could not rebuild kept work onto {branch}: {report.detail}"
        logger.error("[%s] %s", AGENT, message)
        return False, message
    return True, ""


def wave_orchestrator_node(state: PipelineState) -> dict:
    """Dispatch the current wave's tasks, each in its own worktree."""
    artifacts = art.prepare(state["run_id"], state["target_repo"])
    target = state["target_repo"]
    wave_index = state.get("current_wave", 0)
    waves = state.get("waves") or []

    if wave_index >= len(waves):
        return {
            "status": "failed",
            "stop_reason": f"Wave {wave_index} was requested but the plan has {len(waves)}.",
            "events": [event("wave_failed", agent=AGENT, wave=wave_index, error_kind="no_such_wave")],
        }

    attempt = _attempt_number(state, wave_index)
    wave_task_ids = waves[wave_index]
    by_id = {task["task_id"]: task for task in state.get("tasks") or []}

    if not any(task_id in by_id for task_id in wave_task_ids):
        return {
            "status": "failed",
            "stop_reason": (
                f"Wave {wave_index} names tasks {wave_task_ids} and none of them is in the plan."
            ),
            "events": [event("wave_failed", agent=AGENT, wave=wave_index, error_kind="no_tasks")],
        }

    # Every wave branches from the integration branch: wave 0 from the repository
    # as it stands, wave N from whatever the merger has integrated so far.
    base, branch_result = wt.ensure_integration_branch(target, state["run_id"])
    if not branch_result.ok:
        return {
            "status": "failed",
            "stop_reason": f"Could not prepare the integration branch: {branch_result.stderr}",
            "events": [event("wave_failed", agent=AGENT, wave=wave_index, error_kind="git")],
        }

    # On a rework, only tasks not currently "kept" get redispatched — a task's
    # latest recorded verdict across every attempt of this wave so far decides
    # that (docs/Bugs.md #35: this used to be the whole wave, unconditionally).
    # No verdicts recorded at all (ENABLE_REVIEWER off, or a checkpoint from
    # before this existed) degrades to the old "redispatch everything" — every
    # task's lookup simply misses "keep".
    task_ids = wave_task_ids
    rework_comments: dict[str, str] = {}
    if state.get("review_verdict") == "rework":
        wave_level_fallback = state.get("review_comments", "")
        verdicts = latest_task_verdicts(state.get("wave_results") or [], wave_index)
        kept_ids = [tid for tid in wave_task_ids if verdicts.get(tid, {}).get("verdict") == "keep"]
        task_ids = [tid for tid in wave_task_ids if tid not in kept_ids]
        rework_comments = {
            tid: verdicts.get(tid, {}).get("reason") or wave_level_fallback for tid in task_ids
        }
        rebuilt_ok, rebuild_error = _rebuild_after_rework(
            state, base, wave_index=wave_index, kept_ids=kept_ids, artifacts=artifacts
        )
        if not rebuilt_ok:
            return {
                "status": "failed",
                "stop_reason": f"Could not rebuild the integration branch after rework: {rebuild_error}",
                "events": [event("wave_failed", agent=AGENT, wave=wave_index, error_kind="rebuild_failed")],
            }

    tasks = [by_id[task_id] for task_id in task_ids if task_id in by_id]
    if not tasks:
        return {
            "status": "failed",
            "stop_reason": (
                f"Wave {wave_index} attempt {attempt} has nothing left to dispatch — every "
                "task is either already kept or missing from the plan."
            ),
            "events": [event("wave_failed", agent=AGENT, wave=wave_index, error_kind="no_tasks")],
        }

    # Where this wave started, so a later rework can put the branch back. Captured
    # on the first attempt only — a rework must return to the wave's own starting
    # point, not to wherever the attempt being discarded happened to leave it.
    # Read from the branch rather than from HEAD: the target repository may be
    # checked out somewhere else entirely.
    base_sha = state.get("wave_base_sha") or wt.git(target, "rev-parse", base).stdout
    # The run's own starting point, captured once. A replan resets the branch all
    # the way back here.
    run_base_sha = state.get("run_base_sha") or base_sha

    logger.info(
        "[%s] wave %d, attempt %d | %d task(s): %s | base=%s",
        AGENT,
        wave_index,
        attempt,
        len(tasks),
        ", ".join(task_ids),
        base_sha[:8] or "?",
    )
    art.append_event(
        artifacts,
        event("wave_started", agent=AGENT, wave=wave_index, attempt=attempt, tasks=task_ids),
    )

    # Every task dispatched in an earlier wave, so the roster's slot index keeps
    # advancing across the run instead of restarting at 0 each wave — see
    # run_wave's docstring for why a bare per-wave index cannot honour a roster
    # the planner ordered deliberately (e.g. experts first, small/medium after).
    agent_offset = sum(len(wave) for wave in waves[:wave_index])
    # Each task's permanent position within the wave, not its position in this
    # (possibly narrowed) dispatch — see run_wave's docstring for why that
    # distinction matters once a rework can dispatch a subset.
    task_slots = {task_id: wave_task_ids.index(task_id) for task_id in task_ids}

    outcomes = run_wave(
        tasks,
        target_repo=target,
        run_id=state["run_id"],
        base=base,
        coding_agents=_coding_agents_from(state),
        agent_offset=agent_offset,
        task_slots=task_slots,
        rework_comments=rework_comments,
        sessions=_sessions_from(state, wave_index) if rework_comments else {},
    )

    succeeded = [outcome for outcome in outcomes if outcome.ok]
    failed = [outcome for outcome in outcomes if not outcome.ok]
    cost = sum(outcome.cost_usd for outcome in outcomes)

    for outcome in outcomes:
        logger.info("[%s] %s", AGENT, outcome.evidence())

    wave_result = {
        "wave": wave_index,
        "attempt": attempt,
        "task_ids": task_ids,
        "tasks": [outcome.as_dict() for outcome in outcomes],
        "merged": False,
    }

    entry = event(
        "wave_finished",
        agent=AGENT,
        wave=wave_index,
        attempt=attempt,
        succeeded=len(succeeded),
        failed=len(failed),
        cost_usd=round(cost, 4),
    )
    art.append_event(artifacts, entry)

    update: dict = {
        "wave_results": [wave_result],
        "wave_base_sha": base_sha,
        "run_base_sha": run_base_sha,
        "active_worktrees": [
            {
                "task_id": outcome.task_id,
                "branch": outcome.branch,
                "worktree": outcome.worktree,
                "commit": outcome.commit,
                "ok": outcome.ok,
            }
            for outcome in outcomes
        ],
        "total_cost_usd": state.get("total_cost_usd", 0.0) + cost,
        "events": [entry],
        # Consumed. Leaving it set would make the next wave think it was a rework.
        "review_verdict": None,
        "review_comments": "",
    }

    if not succeeded:
        # Nothing merged, nothing to review. Stop rather than hand the merger an
        # empty wave and let the run report a clean finish having built nothing.
        reasons = "; ".join(
            f"{outcome.task_id}: {outcome.error_kind or 'blocked'}" for outcome in failed
        )
        update["status"] = "failed"
        update["stop_reason"] = f"Every task in wave {wave_index} failed. {reasons}"
        logger.error("[%s] wave %d produced nothing usable", AGENT, wave_index)

    return update
