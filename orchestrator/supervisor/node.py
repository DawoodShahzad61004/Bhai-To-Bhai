"""Agent 6 — supervisor.

    Review -> [Supervisor] -> END
                 │
                 └── replan ──> Plan

Runs once, when every wave has completed and merged — it sits outside the
Orchestrate → Merge → Review inner loop. Larger model: this box decides whether
the whole thing satisfies the requirements.

Switched off by `ENABLE_SUPERVISOR = False` in config.py, which removes the node
from the graph and ends the run when the last wave merges.

**Why its feedback goes to the planner.** The reviewer rejects an implementation:
the plan was fine, the work was not. The supervisor rejects a *result*: the work
may have been fine and the plan wrong or incomplete. Sending supervisor feedback
to the coding agents would ask them to fix something they did correctly.

**The open question this closes.** docs/Architecture.md, "Supervisor" notes that
if the supervisor finds the requirements themselves were misunderstood, replanning
against the same context.md reproduces the misunderstanding — and that whether
that should escalate to the user is unspecified. It escalates: `MAX_REPLAN_ROUNDS`
bounds the loop, and reaching it stops the run with status `bounded` and the
unmet requirements named, which puts the decision in front of a person rather
than spending another full pass to arrive at the same place.
"""

from __future__ import annotations

import artifacts as art
import config
import parsing
from adapters import run_agent
from logging_config import get_logger
from state import PipelineState, event
from supervisor.prompts import SUPERVISOR_FRAME, supervisor_prompt

logger = get_logger(__name__)

AGENT = "supervisor"

# Read-only. The supervisor assesses; the planner and coding agents act.
SUPERVISOR_TOOLS = ("Read", "Glob", "Grep", "Bash")


def _waves_summary(state: PipelineState) -> list[str]:
    """One block per wave: what it was for and what came of it."""
    lines = []
    for record in state.get("wave_results") or []:
        if not record.get("merged"):
            # An unmerged attempt was superseded by a rework; the merged one
            # further down the list is what actually landed.
            continue
        wave = record.get("wave")
        verdict = record.get("review_verdict") or "not reviewed"
        lines.append(f"\n### Wave {wave} (review: {verdict})")
        for task in record.get("tasks", []):
            changed = ", ".join(task.get("changed_files") or []) or "nothing"
            status = "ok" if task.get("ok") else f"FAILED ({task.get('error_kind') or 'blocked'})"
            report = (task.get("report") or "").replace("\n", " ")[:200]
            lines.append(f"- {task.get('task_id')} [{status}] changed {changed} — {report}")
    return lines


def _write_notes(artifacts, *, attempt: int, verdict: str, payload: dict) -> None:
    unmet = parsing.string_list(payload, "unmet")
    lines = [
        f"# Supervisor assessment — attempt {attempt}",
        "",
        f"**Verdict:** {verdict}",
        "",
        "## Assessment",
        "",
        (parsing.require_str(payload, "assessment") or "_none given_").strip(),
    ]
    if unmet:
        lines += ["", "## Unmet requirements", ""]
        lines += [f"- {item}" for item in unmet]
    guidance = parsing.require_str(payload, "replan_guidance")
    if guidance:
        lines += ["", "## Guidance for the planner", "", guidance.strip()]

    art.write_text(artifacts.supervisor_file(attempt), "\n".join(lines).rstrip() + "\n")


def _replan_text(payload: dict) -> str:
    """What the planner sees. It sees this and nothing else about the decision."""
    parts = []
    guidance = parsing.require_str(payload, "replan_guidance")
    if guidance:
        parts.append(guidance.strip())
    unmet = parsing.string_list(payload, "unmet")
    if unmet:
        parts.append("Requirements not satisfied by the finished result:\n" +
                     "\n".join(f"- {item}" for item in unmet))
    if not parts:
        parts.append(
            parsing.require_str(payload, "assessment")
            or "The supervisor rejected the result without giving a reason."
        )
    return "\n\n".join(parts)


def supervisor_node(state: PipelineState) -> dict:
    """Assess the finished result against the original requirements."""
    artifacts = art.prepare(state["run_dir"])
    replan_count = state.get("replan_count", 0)
    waves = state.get("waves") or []

    logger.info("[%s] assessing the finished result (attempt %d)", AGENT, replan_count)

    plan = art.read_json(artifacts.plan, default={}) or {}
    result = run_agent(
        supervisor_prompt(
            waves=len(waves),
            branch=state.get("integration_branch") or "the integration branch",
            context_path=str(artifacts.context),
            user_choices_path=str(artifacts.user_choices),
            plan_summary=plan.get("summary", ""),
            waves_summary=_waves_summary(state),
        ),
        spec=config.AGENTS[AGENT],
        system_prompt=SUPERVISOR_FRAME,
        cwd=state["target_repo"],
        tag=AGENT,
        tools=SUPERVISOR_TOOLS,
        extra_dirs=(state["run_dir"],),
    )
    cost = result.cost_usd
    running_cost = state.get("total_cost_usd", 0.0) + cost

    if not result.ok:
        message = f"The supervising agent failed. {result.error_message}"
        logger.error("[%s] %s", AGENT, message)
        return {
            "status": "failed",
            "stop_reason": message,
            "total_cost_usd": running_cost,
            "events": [event("supervision_failed", agent=AGENT, error_kind=result.error_kind, detail=message)],
        }

    parsed = parsing.extract_json(result.text, result.structured)
    payload = parsed.value or {}
    verdict = parsing.one_of(payload, "verdict", ("accepted", "replan"))

    if verdict is None:
        # An unreadable supervisor has accepted nothing. This is the last gate,
        # and coercing it to "accepted" would let a run declare itself finished
        # on the strength of a reply nobody could read.
        message = (
            "The supervising agent's verdict could not be read, so the result has "
            f"not been accepted. {parsed.error or 'no usable verdict field'}"
        )
        logger.error("[%s] %s", AGENT, message)
        return {
            "status": "failed",
            "stop_reason": message,
            "total_cost_usd": running_cost,
            "events": [event("supervision_failed", agent=AGENT, error_kind="unparseable", detail=message)],
        }

    _write_notes(artifacts, attempt=replan_count, verdict=verdict, payload=payload)

    learning = parsing.require_str(payload, "learnings", allow_empty=True)
    if learning:
        art.append_learning(artifacts, AGENT, learning)

    entry = event(
        "supervisor_verdict",
        agent=AGENT,
        verdict=verdict,
        attempt=replan_count,
        unmet=len(parsing.string_list(payload, "unmet")),
        cost_usd=round(cost, 4),
    )
    art.append_event(artifacts, entry)

    update: dict = {
        "supervisor_verdict": verdict,
        "total_cost_usd": running_cost,
        "events": [entry],
    }

    if verdict == "accepted":
        logger.info("[%s] accepted | total $%.4f", AGENT, running_cost)
        update["status"] = "completed"
        update["stop_reason"] = "The supervisor accepted the result against the original requirements."
        update["supervisor_comments"] = ""
        return update

    guidance = _replan_text(payload)
    update["supervisor_comments"] = guidance

    if replan_count >= config.MAX_REPLAN_ROUNDS:
        reason = (
            f"The supervisor rejected the result after {replan_count} replan(s) and "
            f"MAX_REPLAN_ROUNDS is {config.MAX_REPLAN_ROUNDS}. Stopped with "
            f"requirements unmet — this is not a successful run. Outstanding: "
            f"{guidance[:400]}"
        )
        logger.warning("[%s] %s", AGENT, reason)
        bound = event(
            "replan_bound_reached",
            agent=AGENT,
            replan_count=replan_count,
            limit=config.MAX_REPLAN_ROUNDS,
        )
        art.append_event(artifacts, bound)
        art.append_learning(
            artifacts,
            AGENT,
            f"The goal was not reached in {config.MAX_REPLAN_ROUNDS + 1} planning "
            f"attempt(s). Outstanding:\n{guidance}",
        )
        update["status"] = "bounded"
        update["stop_reason"] = reason
        update["events"] = [entry, bound]
        return update

    logger.info(
        "[%s] sending back to the planner (replan %d of %d)",
        AGENT,
        replan_count + 1,
        config.MAX_REPLAN_ROUNDS,
    )
    update["replan_count"] = replan_count + 1
    return update
