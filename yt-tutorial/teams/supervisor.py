"""Supervisor routing and the guards that make agent loops terminate.

Small models on TGI are grammar-constrained to emit a tool call on every turn
while tools are attached. They therefore never produce a final answer, and they
re-select a worker that has already reported. Prompt instructions do not fix
either behaviour -- both guards below are structural for that reason.
"""

from typing import List, Literal

from langchain.agents.middleware import ToolCallLimitMiddleware
from langgraph.graph import END
from langgraph.types import Command
from typing_extensions import TypedDict

from config import MAX_WORKER_REPORTS, STRUCTURED_OUTPUT_METHOD, TOOL_CALL_RUN_LIMIT
from llm.invoker import llm_invoke
from logging_config import get_logger
from prompts import SUPERVISOR_BASE, SUPERVISOR_TERMINATION_RULES
from teams.state import State

logger = get_logger(__name__)


def tool_call_limit(run_limit: int = TOOL_CALL_RUN_LIMIT) -> ToolCallLimitMiddleware:
    """Bound one worker's tool calls per invocation, then end its run."""
    return ToolCallLimitMiddleware(run_limit=run_limit, exit_behavior="end")


def agent_report(result: dict) -> str:
    """Describe what a worker actually did.

    When the tool-call limit ends a run, the final message is a bookkeeping stub
    ("Tool call limit reached: ..."). Handing that to the supervisor reads as
    unfinished work and it routes straight back, so report the tool calls
    instead -- that is the part the supervisor can act on.
    """
    messages = result["messages"]
    text = (messages[-1].content or "").strip()
    if text and not text.startswith("Tool call limit reached"):
        return text

    actions = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            target = call["args"].get("file_name", "")
            actions.append(f"{call['name']}({target})" if target else call["name"])
    if not actions:
        return "No action taken."
    return f"Finished. Tools executed: {', '.join(actions)}."


def make_supervisor_node(
    llm,
    members: List[str],
    scope: str = "",
    max_worker_reports: int = MAX_WORKER_REPORTS,
):
    """Build a supervisor node that routes among `members` or finishes.

    `scope` states what this team cannot do. A team is handed the whole user
    request, including parts its workers have no tools for, and without a stated
    remit the supervisor chases work it structurally cannot complete.
    """
    options = ["FINISH"] + members
    team_label = "/".join(members)
    system_prompt = SUPERVISOR_BASE.format(members=members)
    if scope:
        system_prompt += f" {scope}"
    system_prompt += SUPERVISOR_TERMINATION_RULES
    logger.debug("Built supervisor over %s (max_worker_reports=%d)", members, max_worker_reports)

    class Router(TypedDict):
        """Worker to route to next. Route to FINISH when no more work is needed."""

        next: Literal[*options]

    # TGI answers 500 for response_format json_schema/json_object -- langchain's
    # default -- but handles tool calling correctly.
    router_llm = llm.with_structured_output(Router, method=STRUCTURED_OUTPUT_METHOD)

    def supervisor(state: State) -> Command[Literal[*members, END]]:
        messages = [{"role": "system", "content": system_prompt}] + state["messages"]
        logger.debug(
            "Supervisor(%s) routing over %d message(s)", team_label, len(state["messages"])
        )

        result = llm_invoke(router_llm, messages, caller_tag="supervisor")
        if not result.ok:
            logger.error(
                "Supervisor(%s) routing call failed: %s - %s",
                team_label,
                result.error_kind,
                result.error_message,
            )
            raise RuntimeError(
                f"Supervisor routing call failed ({result.error_kind}): {result.error_message}"
            )

        goto = result.response["next"]
        if goto == "FINISH":
            logger.info("Supervisor(%s) decided FINISH", team_label)
            return Command(goto=END)

        # The router re-selects a worker that has already reported, so a request
        # this team cannot satisfy loops until the recursion limit. Saying so in
        # the system prompt is not enough -- cap the repeats structurally.
        reports = sum(1 for m in state["messages"] if getattr(m, "name", None) == goto)
        if reports >= max_worker_reports:
            logger.warning(
                "Supervisor(%s) chose '%s' which already reported %d time(s); "
                "forcing FINISH (max_worker_reports=%d)",
                team_label,
                goto,
                reports,
                max_worker_reports,
            )
            return Command(goto=END)

        logger.info("Supervisor(%s) routing to '%s'", team_label, goto)
        return Command(goto=goto, update={"next": goto})

    return supervisor
