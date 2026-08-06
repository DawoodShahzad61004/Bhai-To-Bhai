"""A supervisor that asks Claude Code where to route next.

Drop-in for teams/supervisor.py's make_supervisor_node: same arguments, same
Command back, same system prompt assembled the same way. The routing question
put to Claude Code is therefore word for word the one the LangChain supervisor
answers, and the two can be swapped between runs of the same graph.

Two things are not delegated. The router is given no tools at all, because its
whole job is to name a worker. And the repeat guard from teams/supervisor.py is
kept, because it is the only thing standing between a supervisor that will not
stop and a run that bills for every turn until the recursion limit.
"""

from __future__ import annotations

import json
from typing import List, Literal

from langgraph.graph import END
from langgraph.types import Command

from claude_agents import prompts as framing
from claude_agents.cli import run_claude
from config import MAX_WORKER_REPORTS
from logging_config import get_logger
from prompts import SUPERVISOR_BASE, SUPERVISOR_TERMINATION_RULES
from teams.state import State

logger = get_logger(__name__)


def _router_schema(options: List[str]) -> dict:
    """The same Router TypedDict teams/supervisor.py binds, as JSON Schema.

    Handed to the CLI as --json-schema, which constrains the reply to one of the
    listed names. The LangChain path gets this guarantee from tool calling; here
    it comes from the CLI, and without it a strong model narrates its reasoning
    and the routing decision has to be guessed out of prose.
    """
    return {
        "type": "object",
        "properties": {"next": {"type": "string", "enum": options}},
        "required": ["next"],
        "additionalProperties": False,
    }


def _decision(result, options: List[str]) -> str | None:
    """The chosen worker, or None if the reply did not contain one."""
    payload = result.structured
    if not isinstance(payload, dict):
        try:
            payload = json.loads(result.text)
        except (json.JSONDecodeError, TypeError):
            return None
    choice = payload.get("next") if isinstance(payload, dict) else None
    return choice if choice in options else None


def make_claude_supervisor_node(
    members: List[str],
    scope: str = "",
    max_worker_reports: int = MAX_WORKER_REPORTS,
):
    """Build a supervisor node that routes among `members` or finishes."""
    options = ["FINISH"] + members
    team_label = "/".join(members)
    role = SUPERVISOR_BASE.format(members=members)
    if scope:
        role += f" {scope}"
    role += SUPERVISOR_TERMINATION_RULES
    system_prompt = framing.ROUTER_FRAME.format(role=role)
    schema = _router_schema(options)
    logger.debug(
        "Built claude supervisor over %s (max_worker_reports=%d)", members, max_worker_reports
    )

    def supervisor(state: State) -> Command[Literal[*members, END]]:
        logger.debug(
            "ClaudeSupervisor(%s) routing over %d message(s)",
            team_label,
            len(state["messages"]),
        )
        result = run_claude(
            framing.ROUTER_REQUEST.format(transcript=framing.transcript(state["messages"])),
            system_prompt=system_prompt,
            tag=f"supervisor-{team_label}",
            json_schema=schema,
        )
        if not result.ok:
            logger.error(
                "ClaudeSupervisor(%s) routing call failed: %s - %s",
                team_label,
                result.error_kind,
                result.error_message,
            )
            raise RuntimeError(
                f"Claude Code routing call failed ({result.error_kind}): {result.error_message}"
            )

        goto = _decision(result, options)
        if goto is None:
            # Ending the team is the safe reading: whatever it has produced so
            # far stays in the transcript for the next one, whereas guessing a
            # worker spends a turn on a decision nobody made.
            logger.error(
                "ClaudeSupervisor(%s) returned no usable choice from %r; forcing FINISH",
                team_label,
                result.text[:200],
            )
            return Command(goto=END)

        if goto == "FINISH":
            logger.info("ClaudeSupervisor(%s) decided FINISH", team_label)
            return Command(goto=END)

        # Same structural cap as teams/supervisor.py. A router that re-selects a
        # worker which has already reported loops until the recursion limit, and
        # every turn of that loop is a paid CLI invocation.
        reports = sum(1 for m in state["messages"] if getattr(m, "name", None) == goto)
        if reports >= max_worker_reports:
            logger.warning(
                "ClaudeSupervisor(%s) chose '%s' which already reported %d time(s); "
                "forcing FINISH (max_worker_reports=%d)",
                team_label,
                goto,
                reports,
                max_worker_reports,
            )
            return Command(goto=END)

        logger.info("ClaudeSupervisor(%s) routing to '%s'", team_label, goto)
        return Command(goto=goto, update={"next": goto})

    return supervisor
