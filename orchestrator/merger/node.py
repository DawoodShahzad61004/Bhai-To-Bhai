"""Agent 4 — merger.

    Orchestrate -> [Merge] -> Review

Runs after each wave, not once at the end: the coding subagents in a wave work in
worktrees that cannot see each other, so their branches have to be brought
together before anything can be reviewed as a whole.

Smaller model, and it usually costs nothing at all — a wave whose branches merge
cleanly never invokes an agent. The merge agent is dispatched only into a
conflict git could not resolve itself, which makes this the cheapest of the six
boxes in the common case and the one with the most interesting failure mode in
the uncommon one.
"""

from __future__ import annotations

import artifacts as art
import worktrees as wt
from logging_config import get_logger
from merger.merge import merge_wave
from state import PipelineState, event

logger = get_logger(__name__)

AGENT = "merger"


def merger_node(state: PipelineState) -> dict:
    """Merge the current wave's branches into the integration branch."""
    artifacts = art.prepare(state["run_id"], state["target_repo"])
    target = state["target_repo"]
    wave_index = state.get("current_wave", 0)

    results = state.get("wave_results") or []
    if not results:
        return {
            "status": "failed",
            "stop_reason": "The merger was reached with no wave to merge.",
            "events": [event("merge_failed", agent=AGENT, error_kind="no_wave")],
        }
    latest = results[-1]

    into, branch_result = wt.ensure_integration_branch(target, state["run_id"])
    if not branch_result.ok:
        return {
            "status": "failed",
            "stop_reason": f"Could not prepare the integration branch: {branch_result.stderr}",
            "events": [event("merge_failed", agent=AGENT, error_kind="git")],
        }

    logger.info("[%s] merging wave %d into %s", AGENT, wave_index, into)

    report = merge_wave(
        target_repo=target,
        into=into,
        tasks=latest.get("tasks", []),
        context_path=str(artifacts.context),
        artifacts_dir=str(artifacts.shared_dir),
    )

    for learning in report.learnings:
        art.append_learning(artifacts, AGENT, learning)

    entry = event(
        "wave_merged" if report.ok else "merge_failed",
        agent=AGENT,
        wave=wave_index,
        merged=report.merged,
        skipped=report.skipped,
        conflicts_resolved=report.conflicts_resolved,
        unresolved=report.unresolved,
        cost_usd=round(report.cost_usd, 4),
    )
    art.append_event(artifacts, entry)

    # Folded into the attempt this merge belongs to rather than appended beside
    # it. `wave_results` upserts on (wave, attempt) precisely so that these two
    # writers — the orchestrator that dispatched the attempt and the merger that
    # closed it — end up with one row rather than two that disagree.
    update: dict = {
        "wave_results": [
            {
                "wave": latest.get("wave"),
                "attempt": latest.get("attempt"),
                "merged": report.ok,
                "merge_conflicts": report.unresolved,
                "merge_detail": report.detail,
            }
        ],
        "merge_ok": report.ok,
        "merge_report": report.detail,
        "integration_branch": into,
        "total_cost_usd": state.get("total_cost_usd", 0.0) + report.cost_usd,
        "events": [entry],
    }

    if report.ok:
        logger.info("[%s] wave %d merged | %s", AGENT, wave_index, report.detail)
    else:
        update["status"] = "failed"
        update["stop_reason"] = f"Wave {wave_index} could not be merged. {report.detail}"
        logger.error("[%s] %s", AGENT, update["stop_reason"])

    return update
