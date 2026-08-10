"""Agent 5 — reviewer.

    Merge -> [Review] -> Supervisor
               │
               └── rework ──> Orchestrate

Runs once per wave, against the merged result. Larger model: this box decides
whether an implementation is acceptable, which is judgment, and it is one of only
two boxes allowed to send the pipeline backwards.

Switched off by `ENABLE_REVIEWER = False` in config.py, which removes the node
from the graph entirely — the rework loop disappears with it and a wave is
accepted the moment it merges.

**The bound.** `MAX_REWORK_ROUNDS` caps how many times one wave may be sent back.
When it is reached the run stops with status `bounded`, and that word is doing
real work: docs/Bugs.md #15 is a run that stopped because a guard fired and
recorded it exactly as it would have recorded finishing, so the audit trail
reported unfinished work as complete. "Stopped because done" and "stopped because
bounded" are different outcomes and are written down differently here.

Stopping is deliberate rather than continuing with known-bad work. The reviewer
has already said, twice, that this wave is wrong; passing it to a supervisor that
would reject it again spends the most expensive call in the pipeline to learn
something already known.
"""

from __future__ import annotations

import artifacts as art
import config
import parsing
from adapters import run_agent
from logging_config import get_logger
from reviewer.prompts import REVIEW_FRAME, review_prompt
from state import PipelineState, event

logger = get_logger(__name__)

AGENT = "reviewer"

# Read-only. The reviewer reviews; the coding agents fix.
REVIEW_TOOLS = ("Read", "Glob", "Grep", "Bash")


def _evidence_lines(wave_record: dict) -> list[str]:
    """One line per task, separating what was claimed from what git observed."""
    lines = []
    for task in wave_record.get("tasks", []):
        task_id = task.get("task_id", "?")
        if not task.get("ok"):
            lines.append(
                f"{task_id}: FAILED ({task.get('error_kind') or 'blocked'}) — "
                f"{(task.get('error_message') or task.get('report') or '')[:200]}"
            )
            continue
        claimed = ", ".join(task.get("claimed_files") or []) or "nothing"
        changed = ", ".join(task.get("changed_files") or []) or "NOTHING"
        report = (task.get("report") or "").replace("\n", " ")[:300]
        lines.append(
            f"{task_id}: said \"{report}\" | claimed to change {claimed} | "
            f"git saw changes to {changed}"
        )
    return lines


def _write_notes(artifacts, *, wave: int, attempt: int, verdict: str, payload: dict) -> str:
    """Persist the review as markdown, per the drawing's "writes in the md file"."""
    problems = parsing.string_list(payload, "problems")
    lines = [
        f"# Review — wave {wave}, attempt {attempt}",
        "",
        f"**Verdict:** {verdict}",
        "",
        "## Assessment",
        "",
        (parsing.require_str(payload, "assessment") or "_none given_").strip(),
    ]
    if problems:
        lines += ["", "## Problems", ""]
        lines += [f"- {problem}" for problem in problems]
    instructions = parsing.require_str(payload, "rework_instructions")
    if instructions:
        lines += ["", "## Rework instructions", "", instructions.strip()]

    body = "\n".join(lines).rstrip() + "\n"
    art.write_text(artifacts.review_file(wave, attempt), body)
    return body


def _rework_text(payload: dict) -> str:
    """What goes back to the coding agents: instructions plus the problem list."""
    parts = []
    instructions = parsing.require_str(payload, "rework_instructions")
    if instructions:
        parts.append(instructions.strip())
    problems = parsing.string_list(payload, "problems")
    if problems:
        parts.append("\n".join(f"- {problem}" for problem in problems))
    if not parts:
        parts.append(parsing.require_str(payload, "assessment") or "The reviewer rejected this work.")
    return "\n\n".join(parts)


def reviewer_node(state: PipelineState) -> dict:
    """Review the merged wave against its task files and the original context."""
    artifacts = art.prepare(state["run_dir"])
    wave_index = state.get("current_wave", 0)
    results = state.get("wave_results") or []

    if not results:
        return {
            "status": "failed",
            "stop_reason": "The reviewer was reached with no wave to review.",
            "events": [event("review_failed", agent=AGENT, error_kind="no_wave")],
        }

    latest = results[-1]
    attempt = latest.get("attempt", 0)
    task_ids = latest.get("task_ids", [])
    by_id = {task["task_id"]: task for task in state.get("tasks") or []}
    tasks = [by_id[task_id] for task_id in task_ids if task_id in by_id]

    logger.info("[%s] reviewing wave %d, attempt %d", AGENT, wave_index, attempt)

    result = run_agent(
        review_prompt(
            wave=wave_index,
            branch=state.get("integration_branch") or "the integration branch",
            tasks=tasks,
            evidence=_evidence_lines(latest),
            context_path=str(artifacts.context),
        ),
        spec=config.AGENTS[AGENT],
        system_prompt=REVIEW_FRAME,
        cwd=state["target_repo"],
        tag=AGENT,
        tools=REVIEW_TOOLS,
        extra_dirs=(state["run_dir"],),
    )
    cost = result.cost_usd

    if not result.ok:
        message = f"The review agent failed. {result.error_message}"
        logger.error("[%s] %s", AGENT, message)
        return {
            "status": "failed",
            "stop_reason": message,
            "total_cost_usd": state.get("total_cost_usd", 0.0) + cost,
            "events": [event("review_failed", agent=AGENT, error_kind=result.error_kind, detail=message)],
        }

    parsed = parsing.extract_json(result.text, result.structured)
    payload = parsed.value or {}
    verdict = parsing.one_of(payload, "verdict", ("approved", "rework"))

    if verdict is None:
        # An unreadable reviewer has not approved anything. Coercing this to
        # "approved" would be the pipeline agreeing with itself about work
        # nothing actually assessed.
        message = (
            "The review agent's verdict could not be read, so nothing has been "
            f"approved. {parsed.error or 'no usable verdict field'}"
        )
        logger.error("[%s] %s", AGENT, message)
        return {
            "status": "failed",
            "stop_reason": message,
            "total_cost_usd": state.get("total_cost_usd", 0.0) + cost,
            "events": [event("review_failed", agent=AGENT, error_kind="unparseable", detail=message)],
        }

    _write_notes(artifacts, wave=wave_index, attempt=attempt, verdict=verdict, payload=payload)

    learning = parsing.require_str(payload, "learnings", allow_empty=True)
    if learning:
        art.append_learning(artifacts, AGENT, learning)

    rework_count = state.get("rework_count", 0)
    entry = event(
        "review_verdict",
        agent=AGENT,
        wave=wave_index,
        attempt=attempt,
        verdict=verdict,
        problems=len(parsing.string_list(payload, "problems")),
        cost_usd=round(cost, 4),
    )
    art.append_event(artifacts, entry)

    update: dict = {
        "review_verdict": verdict,
        "wave_results": [
            {
                "wave": wave_index,
                "attempt": attempt,
                "review_verdict": verdict,
                "review_comments": _rework_text(payload) if verdict == "rework" else "",
            }
        ],
        "total_cost_usd": state.get("total_cost_usd", 0.0) + cost,
        "events": [entry],
    }

    if verdict == "approved":
        logger.info("[%s] wave %d approved", AGENT, wave_index)
        update["review_comments"] = ""
        # A wave that passes resets the budget for the next one: the bound is
        # per wave, not per run.
        update["rework_count"] = 0
        return update

    comments = _rework_text(payload)
    update["review_comments"] = comments

    if rework_count >= config.MAX_REWORK_ROUNDS:
        reason = (
            f"Wave {wave_index} was sent back for rework {rework_count} time(s) and "
            f"MAX_REWORK_ROUNDS is {config.MAX_REWORK_ROUNDS}. The reviewer still "
            f"rejects it. Stopped with the work incomplete — this is not a "
            f"successful run. Outstanding: {comments[:400]}"
        )
        logger.warning("[%s] %s", AGENT, reason)
        bound = event(
            "rework_bound_reached",
            agent=AGENT,
            wave=wave_index,
            rework_count=rework_count,
            limit=config.MAX_REWORK_ROUNDS,
        )
        art.append_event(artifacts, bound)
        art.append_learning(
            artifacts,
            AGENT,
            f"Wave {wave_index} could not be brought to an acceptable state in "
            f"{config.MAX_REWORK_ROUNDS} rework round(s). Outstanding problems:\n{comments}",
        )
        update["status"] = "bounded"
        update["stop_reason"] = reason
        update["events"] = [entry, bound]
        return update

    logger.info(
        "[%s] wave %d sent back for rework (round %d of %d)",
        AGENT,
        wave_index,
        rework_count + 1,
        config.MAX_REWORK_ROUNDS,
    )
    update["rework_count"] = rework_count + 1
    return update
