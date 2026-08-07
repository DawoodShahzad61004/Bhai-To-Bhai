"""Agent 2 — planner.

    Req. gathering -> [Plan] -> Orchestrate
                       ^
                       └── replan, from the Supervisor

Reads `context.md` and `user_choices.md`, writes `plan.json` and one
`TASK-*.json` per task. Larger model: this box decides what to build.

It is one of the two re-entry points in the drawing — the supervisor sends work
back here when the finished result does not satisfy the requirements, on the
reasoning that the implementation may have been fine and the plan wrong.

The node calls the agent, validates what comes back, and derives the wave
schedule in code (planner/waves.py). A plan that cannot be scheduled is a
failure with the specific reason named, never a plan with the awkward parts
quietly dropped: a dangling dependency silently removed becomes a task running
against a tree that lacks what it was told to build on, and the agent then
invents the missing piece — a wrong result wearing the shape of a right one.
"""

from __future__ import annotations

import artifacts as art
import config
import parsing
import worktrees as wt
from adapters import run_agent
from logging_config import get_logger
from planner.prompts import PIPELINE_FRAME, plan_prompt
from planner.waves import assign_waves, normalise_tasks
from state import PipelineState, event

logger = get_logger(__name__)

AGENT = "planner"

# Read-only. The planner plans; the coding subagents implement.
PLANNER_TOOLS = ("Read", "Glob", "Grep", "Bash")


def _failure(message: str, *, kind: str) -> dict:
    logger.error("[%s] %s", AGENT, message)
    return {
        "status": "failed",
        "stop_reason": message,
        "events": [event("plan_failed", agent=AGENT, error_kind=kind, detail=message)],
    }


def planner_node(state: PipelineState) -> dict:
    """Decompose the requirements into tasks and derive their wave schedule."""
    artifacts = art.prepare(state["run_dir"])
    target = state["target_repo"]
    replan_count = state.get("replan_count", 0)
    comments = state.get("supervisor_comments", "") if replan_count else ""

    if comments:
        logger.info("[%s] re-planning (round %d) against supervisor feedback", AGENT, replan_count)
    else:
        logger.info("[%s] planning", AGENT)

    result = run_agent(
        plan_prompt(
            context=state.get("context") or art.read_text(artifacts.context),
            user_choices=art.read_text(artifacts.user_choices, default="_none recorded_"),
            target_repo=target,
            supervisor_comments=comments,
        ),
        spec=config.AGENTS[AGENT],
        system_prompt=PIPELINE_FRAME,
        cwd=target,
        tag=AGENT,
        tools=PLANNER_TOOLS,
    )
    if not result.ok:
        return _failure(
            f"The planning agent failed. {result.error_message}", kind=result.error_kind
        )

    parsed = parsing.extract_json(result.text, result.structured)
    if not parsed.ok:
        return _failure(
            f"The planning agent's reply could not be read: {parsed.error}",
            kind="unparseable",
        )
    payload = parsed.value or {}

    raw_tasks = parsing.require_list(payload, "tasks")
    if raw_tasks is None:
        return _failure(
            "The plan has no 'tasks' list. Nothing can be dispatched from it.",
            kind="invalid_plan",
        )

    tasks, problems = normalise_tasks(raw_tasks)
    if problems:
        # Recorded rather than swallowed: these tasks were dropped, and the plan
        # that reaches the coding agents is smaller than the one the model wrote.
        logger.warning("[%s] discarded %d malformed task(s)", AGENT, len(problems))
        art.append_learning(
            artifacts,
            AGENT,
            "Tasks discarded from the plan as unusable:\n"
            + "\n".join(f"- {problem}" for problem in problems),
        )

    schedule = assign_waves(tasks)
    if not schedule.ok:
        return _failure(
            f"The plan cannot be scheduled: {schedule.error}", kind="invalid_plan"
        )

    scheduled_tasks = schedule.tasks or []
    waves = schedule.waves or []

    if len(waves) > config.MAX_WAVES:
        return _failure(
            f"The plan needs {len(waves)} waves and MAX_WAVES is {config.MAX_WAVES}. "
            "Either the decomposition is too granular or the bound is too low.",
            kind="bounded",
        )

    plan = {
        "run_id": state["run_id"],
        "goal": state["goal"],
        "target_repo": target,
        "replan_round": replan_count,
        "summary": parsing.require_str(payload, "summary") or "",
        "task_count": len(scheduled_tasks),
        "waves": waves,
        "tasks": scheduled_tasks,
        "discarded": problems,
    }
    art.write_json(artifacts.plan, plan)
    art.write_tasks(artifacts, scheduled_tasks)

    entry = event(
        "plan_ready",
        agent=AGENT,
        tasks=len(scheduled_tasks),
        waves=len(waves),
        discarded=len(problems),
        replan_round=replan_count,
        cost_usd=round(result.cost_usd, 4),
    )
    art.append_event(artifacts, entry)
    logger.info(
        "[%s] plan.json written | %d task(s) in %d wave(s) | %s",
        AGENT,
        len(scheduled_tasks),
        len(waves),
        " -> ".join(f"[{', '.join(wave)}]" for wave in waves),
    )

    update = {
        "plan_path": str(artifacts.plan),
        "tasks": scheduled_tasks,
        "waves": waves,
        # A replan restarts the schedule. Without this the pipeline would resume
        # partway through a wave list that no longer exists.
        "current_wave": 0,
        "rework_count": 0,
        "wave_base_sha": "",
        "total_cost_usd": state.get("total_cost_usd", 0.0) + result.cost_usd,
        "events": [entry],
    }

    if replan_count:
        _rewind_for_replan(state, artifacts)

    return update


def _rewind_for_replan(state: PipelineState, artifacts) -> None:
    """Put the integration branch back to where the run started.

    The supervisor rejected the finished result, and this plan is a fresh
    decomposition of the same goal — not a patch on top of the last one. Running
    it against the implementation it replaces would re-implement whatever the two
    plans have in common, and the pipeline would then see agents "changing
    nothing" because the file they were asked to create is already there.

    Every task branch from the previous attempt survives this, so the rejected
    implementation stays inspectable. Only the integration branch moves.
    """
    base = state.get("run_base_sha", "")
    if not base:
        logger.warning("[%s] no run base recorded; the replan builds on the previous work", AGENT)
        return

    branch = wt.integration_branch_name(state["run_id"])
    result = wt.reset_branch(state["target_repo"], branch, base)
    if result.ok:
        logger.info("[%s] rewound %s to the run's starting commit for the replan", AGENT, branch)
        art.append_event(
            artifacts,
            event("replan_rewind", agent=AGENT, branch=branch, to=base[:8]),
        )
    else:
        logger.warning("[%s] could not rewind %s: %s", AGENT, branch, result.stderr)
