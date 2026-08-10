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
from state import PipelineState, event
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


def _revert_rejected_attempt(state: PipelineState, branch: str) -> None:
    """Undo a rejected wave, as the notes specify: the work is reverted/deleted.

    Two halves, and the second is easy to miss. The worktrees are destroyed, so
    the next attempt starts from a clean checkout rather than re-finding the
    partial state that was just rejected. And the integration branch is reset to
    where it stood before this wave merged — because the reviewer runs *after*
    the merger, so a rejected wave has already been integrated by the time
    anyone says it is wrong. Without the reset, the retry branches from its own
    rejected output and its "changes" are measured against them.

    Each task branch survives both steps, so the rejected attempt stays
    inspectable afterwards.
    """
    stale = state.get("active_worktrees") or []
    if stale:
        logger.info("[%s] discarding %d worktree(s) from the rejected attempt", AGENT, len(stale))
        for record in stale:
            path = record.get("worktree")
            if path:
                wt.remove(state["target_repo"], path)

    base_sha = state.get("wave_base_sha", "")
    if base_sha:
        result = wt.reset_branch(state["target_repo"], branch, base_sha)
        if not result.ok:
            logger.warning(
                "[%s] could not reset %s to %s: %s", AGENT, branch, base_sha[:8], result.stderr
            )


def wave_orchestrator_node(state: PipelineState) -> dict:
    """Dispatch the current wave's tasks, each in its own worktree."""
    artifacts = art.prepare(state["run_dir"])
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
    task_ids = waves[wave_index]
    by_id = {task["task_id"]: task for task in state.get("tasks") or []}
    tasks = [by_id[task_id] for task_id in task_ids if task_id in by_id]

    if not tasks:
        return {
            "status": "failed",
            "stop_reason": (
                f"Wave {wave_index} names tasks {task_ids} and none of them is in the plan."
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

    rework_comments = ""
    if state.get("review_verdict") == "rework":
        rework_comments = state.get("review_comments", "")
        _revert_rejected_attempt(state, base)

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

    outcomes = run_wave(
        tasks,
        target_repo=target,
        run_id=state["run_id"],
        base=base,
        run_dir=state["run_dir"],
        coding_agents=_coding_agents_from(state),
        agent_offset=agent_offset,
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
