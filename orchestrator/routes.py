"""Conditional edges: the two feedback loops, and where a run stops.

Every router here returns a **semantic key** — "rework", "next_wave", "done" —
never a node name. graph.py maps those keys onto whichever nodes actually exist,
which is what lets `ENABLE_REVIEWER` and `ENABLE_SUPERVISOR` remove a stage
without any router knowing about it.

That indirection is not decoration. A router returning a node name has to know
the graph's shape, and a `goto` naming a node that was never added is a
compile-time error — so a router that names nodes directly would have to grow a
config check of its own, in every branch, and the two would drift.

The other rule: a router only routes. Deciding *whether* work is acceptable
belongs to the agent that judged it, and setting `status` belongs to the node
that discovered the reason. By the time control reaches a router, the decision is
already in state and this reads it.
"""

from __future__ import annotations

from logging_config import get_logger
from state import PipelineState

logger = get_logger(__name__)

# A run that has stopped for any reason takes this branch out of every router.
TERMINAL = ("failed", "bounded", "completed")


def _stopped(state: PipelineState) -> bool:
    return state.get("status") in TERMINAL


def after_requirements(state: PipelineState) -> str:
    """Does the survey have questions the user still has to answer?"""
    if _stopped(state):
        return "stop"
    return "clarify" if state.get("survey_questions") else "plan"


def after_plan(state: PipelineState) -> str:
    if _stopped(state):
        return "stop"
    return "orchestrate"


def after_orchestrate(state: PipelineState) -> str:
    if _stopped(state):
        return "stop"
    return "merge"


def after_merge(state: PipelineState) -> str:
    """Review the wave, or — with the reviewer off — go straight on."""
    if _stopped(state):
        return "stop"
    return "review"


def after_review(state: PipelineState) -> str:
    """The Review -> Orchestrate feedback edge, and the wave cursor.

    Three outcomes, and the ordering matters. A bound that fired has already set
    `status`, so it is checked first — otherwise a rejected wave whose rework
    budget is spent would be sent back for a round it is not allowed to have.
    """
    if _stopped(state):
        return "stop"

    if state.get("review_verdict") == "rework":
        return "rework"

    wave = state.get("current_wave", 0)
    waves = state.get("waves") or []
    if wave + 1 < len(waves):
        return "next_wave"
    return "supervise"


def after_supervisor(state: PipelineState) -> str:
    """The Supervisor -> Plan feedback edge."""
    if _stopped(state):
        return "stop"
    return "replan" if state.get("supervisor_verdict") == "replan" else "stop"


def advance_wave(state: PipelineState) -> dict:
    """Move the cursor to the next wave and give it a fresh rework budget.

    A node rather than a router, because it writes state. Kept separate from both
    the reviewer and the orchestrator so that "which wave are we on" has exactly
    one writer — with the reviewer switched off there would otherwise be two, and
    they would disagree the first time a bound fired.
    """
    wave = state.get("current_wave", 0) + 1
    logger.info("advancing to wave %d", wave)
    return {
        "current_wave": wave,
        "rework_count": 0,
        "review_verdict": None,
        "review_comments": "",
        # Cleared so the next wave captures its own starting commit. Carrying the
        # previous wave's over would make a rework reset the branch past work
        # that was already accepted.
        "wave_base_sha": "",
    }


def finalise(state: PipelineState) -> dict:
    """Give a run that reached the end without a verdict an explicit outcome.

    Reached when the last wave merges with the supervisor switched off. Without
    this the run would end with `status` still "running", which is the shape of
    docs/Bugs.md #15 — a finished run whose own record does not say what happened.
    The wording names the missing gate, so a result nothing assessed is never
    mistaken for one that passed.
    """
    if state.get("status") in TERMINAL:
        return {}

    waves = len(state.get("waves") or [])
    return {
        "status": "completed",
        "stop_reason": (
            f"All {waves} wave(s) were implemented and merged. No supervisor "
            "assessed the result against the original requirements "
            "(ENABLE_SUPERVISOR is off), so this run reports what was built, not "
            "that it is what was asked for."
        ),
    }
