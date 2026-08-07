"""The pipeline, assembled.

    START -> requirements -> plan -> orchestrate -> merge -> review -> supervise -> END
                    │                    ^                     │           │
                    └── clarify ─┘       └──── rework ─────────┘           │
                                         └──── next wave ──────┘           │
                                  plan <──────── replan ────────────────────┘

Two things about this file are worth reading before changing it.

**The toggles remove nodes, they do not bypass them.** `ENABLE_REVIEWER = False`
means no review node is ever added to the graph, so a disabled stage costs
nothing, appears nowhere in the run log, and cannot be reached by accident. The
routers do not know about the toggles at all: they return semantic keys, and the
edge maps below translate those into whichever nodes exist.

**`Orchestrate -> Merge -> Review` is a loop, not a line.** The drawing shows it
as a single pass; it runs once per wave, and the supervisor sits outside it
(Agent-Pipeline-Diagram.md section 5). `advance_wave` is what closes it.
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

import config
import routes
from logging_config import get_logger
from merger import merger_node
from planner import planner_node
from requirements import requirements_clarify_node, requirements_survey_node
from reviewer import reviewer_node
from state import PipelineState
from supervisor import supervisor_node
from wave_orchestrator import wave_orchestrator_node

logger = get_logger(__name__)

# Node names. Referenced by both the graph and its edge maps, so a typo is a
# compile-time error rather than a routing failure at run time.
REQUIREMENTS = "requirements"
CLARIFY = "requirements_clarify"
PLAN = "plan"
ORCHESTRATE = "orchestrate"
MERGE = "merge"
REVIEW = "review"
ADVANCE = "advance_wave"
SUPERVISE = "supervise"
FINALISE = "finalise"


def build_graph(
    *,
    enable_reviewer: bool | None = None,
    enable_supervisor: bool | None = None,
):
    """Assemble the graph for the current configuration.

    The two flags default to config.py and are parameters so a test can build
    every combination without patching module state.
    """
    reviewer_on = config.ENABLE_REVIEWER if enable_reviewer is None else enable_reviewer
    supervisor_on = config.ENABLE_SUPERVISOR if enable_supervisor is None else enable_supervisor

    builder = StateGraph(PipelineState)

    builder.add_node(REQUIREMENTS, requirements_survey_node)
    builder.add_node(CLARIFY, requirements_clarify_node)
    builder.add_node(PLAN, planner_node)
    builder.add_node(ORCHESTRATE, wave_orchestrator_node)
    builder.add_node(MERGE, merger_node)
    builder.add_node(ADVANCE, routes.advance_wave)
    if reviewer_on:
        builder.add_node(REVIEW, reviewer_node)
    if supervisor_on:
        builder.add_node(SUPERVISE, supervisor_node)
    else:
        # Only reachable when the supervisor is off — it is what gives the run an
        # explicit outcome in the supervisor's absence. Adding it either way would
        # leave an unreachable node sitting in the rendered graph.
        builder.add_node(FINALISE, routes.finalise)

    # ── Requirements ─────────────────────────────────────────────────────────
    builder.add_edge(START, REQUIREMENTS)
    builder.add_conditional_edges(
        REQUIREMENTS,
        routes.after_requirements,
        {"clarify": CLARIFY, "plan": PLAN, "stop": END},
    )
    builder.add_edge(CLARIFY, PLAN)

    # ── Plan -> the wave loop ────────────────────────────────────────────────
    builder.add_conditional_edges(PLAN, routes.after_plan, {"orchestrate": ORCHESTRATE, "stop": END})
    builder.add_conditional_edges(
        ORCHESTRATE, routes.after_orchestrate, {"merge": MERGE, "stop": END}
    )

    # ── Merge -> review, or past it ──────────────────────────────────────────
    # `after_review` decides what happens at the end of a wave: send it back,
    # start the next one, or leave the loop. With the reviewer switched off there
    # is no verdict to route on, so that same decision hangs off the merge —
    # `after_review` reads `review_verdict`, which is simply never set, and the
    # "rework" branch becomes unreachable rather than special-cased.
    if reviewer_on:
        builder.add_conditional_edges(MERGE, routes.after_merge, {"review": REVIEW, "stop": END})
        builder.add_conditional_edges(REVIEW, routes.after_review, _after_review_targets(supervisor_on))
    else:
        builder.add_conditional_edges(MERGE, routes.after_review, _after_review_targets(supervisor_on))

    # ── The wave loop closes here ────────────────────────────────────────────
    builder.add_edge(ADVANCE, ORCHESTRATE)

    # ── Supervisor -> plan, or the end ───────────────────────────────────────
    if supervisor_on:
        builder.add_conditional_edges(
            SUPERVISE, routes.after_supervisor, {"replan": PLAN, "stop": END}
        )
    else:
        builder.add_edge(FINALISE, END)

    logger.debug(
        "graph built | reviewer=%s | supervisor=%s", reviewer_on, supervisor_on
    )
    return builder


def _after_review_targets(supervisor_on: bool) -> dict[str, str]:
    """Where a review decision can send the pipeline.

    "supervise" means "the last wave is done". With the supervisor switched off
    that lands on `finalise`, which gives the run an explicit outcome rather than
    letting it end with `status` still "running".
    """
    return {
        "rework": ORCHESTRATE,
        "next_wave": ADVANCE,
        "supervise": SUPERVISE if supervisor_on else FINALISE,
        "stop": END,
    }


def compile_graph(checkpointer=None, **flags):
    """The runnable graph. A checkpointer is required for the requirements pause."""
    return build_graph(**flags).compile(checkpointer=checkpointer)
