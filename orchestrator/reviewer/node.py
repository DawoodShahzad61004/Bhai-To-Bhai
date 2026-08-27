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
from state import PipelineState, event, latest_task_verdicts

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
        claimed = parsing.joined_and_capped(task.get("claimed_files") or [], empty="nothing")
        changed = parsing.joined_and_capped(task.get("changed_files") or [], empty="NOTHING")
        report = (task.get("report") or "").replace("\n", " ")[:300]
        lines.append(
            f"{task_id}: said \"{report}\" | claimed to change {claimed} | "
            f"git saw changes to {changed}"
        )
    return lines


def _write_notes(
    artifacts,
    *,
    wave: int,
    attempt: int,
    verdict: str,
    payload: dict,
    task_verdicts: dict[str, dict[str, str]],
) -> str:
    """Persist the review as markdown, per the drawing's "writes in the md file"."""
    lines = [
        f"# Review — wave {wave}, attempt {attempt}",
        "",
        f"**Verdict:** {verdict}",
        "",
        "## Assessment",
        "",
        (parsing.require_str(payload, "assessment") or "_none given_").strip(),
        "",
        "## Per-task verdicts",
        "",
    ]
    for task_id, info in task_verdicts.items():
        label = info.get("verdict", "rework").upper()
        reason = (info.get("reason") or "").strip()
        lines.append(f"- {task_id}: {label} — {reason}" if reason else f"- {task_id}: {label}")

    body = "\n".join(lines).rstrip() + "\n"
    art.write_text(artifacts.review_file(wave, attempt), body)
    return body


def _parse_task_verdicts(payload: dict, valid_ids: set[str]) -> dict[str, tuple[str, str]]:
    """task_id -> (verdict, reason), for entries naming a task that actually merged.

    Anything else — an unknown task_id, a missing/invalid verdict, a task the
    reviewer wasn't asked about — is dropped rather than guessed at; the caller
    defaults an absent task to "rework", the same "no usable call ≠ approved"
    rule this pipeline already applies at the wave level.
    """
    verdicts: dict[str, tuple[str, str]] = {}
    for item in parsing.require_list(payload, "task_verdicts") or []:
        if not isinstance(item, dict):
            continue
        task_id = item.get("task_id")
        if not isinstance(task_id, str) or task_id not in valid_ids:
            continue
        verdict = parsing.one_of(item, "verdict", ("keep", "rework"))
        if verdict is None:
            continue
        reason = parsing.require_str(item, "reason", allow_empty=True) or ""
        verdicts[task_id] = (verdict, reason)
    return verdicts


def _rework_text(task_verdicts: dict[str, dict[str, str]]) -> str:
    """The wave-level fallback sent to a redispatched task with no reason of its own."""
    lines = [
        f"- {task_id}: {info['reason']}"
        for task_id, info in task_verdicts.items()
        if info.get("verdict") == "rework" and info.get("reason")
    ]
    return "\n".join(lines) or "The reviewer rejected this work."


def reviewer_node(state: PipelineState) -> dict:
    """Review the merged wave against its task files and the original context."""
    artifacts = art.prepare(state["run_id"], state["target_repo"])
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

    already_kept = [
        task_id
        for task_id, info in latest_task_verdicts(state.get("wave_results") or [], wave_index).items()
        if info.get("verdict") == "keep"
    ]

    result = run_agent(
        review_prompt(
            wave=wave_index,
            branch=state.get("integration_branch") or "the integration branch",
            tasks=tasks,
            evidence=_evidence_lines(latest),
            context_path=str(artifacts.context),
            already_kept=already_kept,
        ),
        spec=config.AGENTS[AGENT],
        system_prompt=REVIEW_FRAME,
        cwd=state["target_repo"],
        tag=AGENT,
        tools=REVIEW_TOOLS,
        extra_dirs=(str(artifacts.shared_dir),),
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
    if not parsed.ok:
        # An unreadable reviewer has not approved anything. Coercing this to
        # "approved" would be the pipeline agreeing with itself about work
        # nothing actually assessed.
        message = f"The review agent's reply could not be read, so nothing has been approved. {parsed.error}"
        logger.error("[%s] %s", AGENT, message)
        return {
            "status": "failed",
            "stop_reason": message,
            "total_cost_usd": state.get("total_cost_usd", 0.0) + cost,
            "events": [event("review_failed", agent=AGENT, error_kind="unparseable", detail=message)],
        }
    payload = parsed.value or {}

    # A per-task verdict, for every task this attempt dispatched. A task that
    # failed outright never merged, so there is no "keep" call to make about
    # it — it is forced back to rework here rather than asked of the model. A
    # task that merged but the model didn't give a usable verdict for defaults
    # to "rework" too, the same "no usable call ≠ approved" rule this pipeline
    # already applied at the wave level before this change.
    merged_ids = {task["task_id"] for task in latest.get("tasks", []) if task.get("ok")}
    parsed_verdicts = _parse_task_verdicts(payload, merged_ids)
    task_verdicts: dict[str, dict[str, str]] = {}
    for task in latest.get("tasks", []):
        task_id = task["task_id"]
        if not task.get("ok"):
            task_verdicts[task_id] = {
                "verdict": "rework",
                "reason": "The task did not produce a mergeable result and will be retried automatically.",
            }
            continue
        call_verdict, reason = parsed_verdicts.get(task_id, ("rework", ""))
        task_verdicts[task_id] = {"verdict": call_verdict, "reason": reason}

    # The wave-level outcome that drives routing is derived, not asked for —
    # so it can never silently disagree with the reviewer's own per-task calls
    # (docs/Bugs.md #35: previously any rework discarded the whole wave;
    # rejecting a task now only means that task).
    verdict = "rework" if any(v["verdict"] == "rework" for v in task_verdicts.values()) else "approved"

    _write_notes(
        artifacts, wave=wave_index, attempt=attempt, verdict=verdict, payload=payload, task_verdicts=task_verdicts
    )

    learning = parsing.require_str(payload, "learnings", allow_empty=True)
    if learning:
        art.append_learning(artifacts, AGENT, learning)

    rework_count = state.get("rework_count", 0)
    kept = sum(1 for info in task_verdicts.values() if info["verdict"] == "keep")
    entry = event(
        "review_verdict",
        agent=AGENT,
        wave=wave_index,
        attempt=attempt,
        verdict=verdict,
        tasks_kept=kept,
        tasks_reworked=len(task_verdicts) - kept,
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
                "review_comments": _rework_text(task_verdicts) if verdict == "rework" else "",
                "task_verdicts": task_verdicts,
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

    comments = _rework_text(task_verdicts)
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
