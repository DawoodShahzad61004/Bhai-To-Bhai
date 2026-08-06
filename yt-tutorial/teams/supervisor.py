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

from config import (
    MAX_WORKER_REPORTS,
    STRUCTURED_OUTPUT_METHOD,
    TOOL_CALL_RUN_LIMIT,
    TOOL_RESULT_SALVAGE_CHARS,
    WORKER_DEADLINE_SECONDS,
    WORKER_ERROR_REPORT_CHARS,
)
from llm.invoker import DeadlineExceededError, llm_invoke, run_with_deadline
from logging_config import get_logger
from prompts import SUPERVISOR_BASE, SUPERVISOR_TERMINATION_RULES
from teams.state import State

logger = get_logger(__name__)


def tool_call_limit(run_limit: int = TOOL_CALL_RUN_LIMIT) -> ToolCallLimitMiddleware:
    """Bound one worker's tool calls per invocation, leaving it a turn to report.

    exit_behavior="continue" blocks further tool calls but keeps the agent loop
    alive, so the worker still gets a turn to write up what it found. "end" stops
    the run the moment the budget is gone: a worker that spent every turn
    searching is cut off before it ever reports, and the supervisor receives a
    bookkeeping stub instead of the findings. Blocked calls still count towards
    the budget, so a model that keeps hammering a blocked tool runs out anyway.
    """
    return ToolCallLimitMiddleware(run_limit=run_limit, exit_behavior="continue")


def single_call_limit(tool_name: str) -> ToolCallLimitMiddleware:
    """Allow exactly one call to `tool_name` per invocation.

    write_document and create_outline replace the whole file, so a second call
    destroys what the first one wrote -- and the last call is reliably the
    thinnest. Instructing the model not to do it does not hold; this makes it
    impossible. Other tools are unaffected, so reading and editing still work.
    """
    return ToolCallLimitMiddleware(
        tool_name=tool_name, run_limit=1, exit_behavior="continue"
    )


def run_worker(agent, name: str, state: State) -> str:
    """Invoke a worker and always come back with a report.

    create_agent calls the model directly, so llm/invoker.py's error handling
    never sees a worker's turns: one bad generation aborts the entire graph.
    A small model on a constrained decoder produces those regularly -- it can
    degenerate mid-argument and the server rejects the request -- and that used
    to discard the work of every team that had already finished. A supervisor
    can route around a worker that failed; it cannot route around a traceback.

    The deadline is wall-clock and covers the whole turn. The client's timeout
    cannot stand in for it: httpx counts idle time between reads, so a server
    that keeps the socket open while it generates never trips it, however long
    it thinks.
    """
    logger.info("--> worker '%s' starting", name)
    try:
        result = run_with_deadline(
            agent.invoke,
            state,
            timeout=WORKER_DEADLINE_SECONDS,
            name=f"worker-{name}",
        )
    except DeadlineExceededError:
        logger.error(
            "Worker '%s' passed its %ds deadline; abandoning the turn",
            name,
            WORKER_DEADLINE_SECONDS,
        )
        return (
            f"Worker '{name}' ran past its {WORKER_DEADLINE_SECONDS}s deadline and "
            "produced no report. Anything it saved before then is still on disk."
        )
    except Exception as e:
        logger.exception("Worker '%s' failed: %s", name, type(e).__name__)
        detail = str(e)[:WORKER_ERROR_REPORT_CHARS]
        return (
            f"Worker '{name}' failed with {type(e).__name__} and produced no report. "
            f"Anything it saved before failing is still on disk. Detail: {detail}"
        )
    report = agent_report(result)
    logger.info("<-- worker '%s' reported: %s", name, report[:200])
    return report


def agent_report(result: dict) -> str:
    """Describe what a worker actually did.

    When the tool-call limit ends a run, the final message is a bookkeeping stub
    ("Tool call limit reached: ..."), but the worker's real answer is usually a
    turn or two further back. Returning the stub reads to the supervisor as
    unfinished work and it routes straight back, so search backwards for the
    last thing the worker actually said.

    The tool calls are appended because prose is only a claim -- a worker saying
    it wrote a file and a write_document call in the transcript are different
    kinds of evidence, and the supervisor is asked to distinguish them.
    """
    messages = result["messages"]

    text = ""
    for message in reversed(messages):
        if getattr(message, "type", None) != "ai":
            continue
        candidate = (message.content or "").strip()
        if candidate and not candidate.startswith("Tool call limit reached"):
            text = candidate
            break

    actions = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            target = call["args"].get("file_name", "")
            actions.append(f"{call['name']}({target})" if target else call["name"])

    if text:
        return f"{text}\n\nTools executed: {', '.join(actions)}." if actions else text

    # No prose anywhere: the worker spent every turn on tools and never wrote up
    # what it found. The tool results are then the only findings that exist, and
    # reporting bare tool names throws the whole invocation's work away -- the
    # next team is left with nothing to write about but the request itself.
    salvaged = _tool_results(messages)
    if salvaged:
        return f"No written report -- raw tool results follow.\n\n{salvaged}"
    if actions:
        return f"Finished. Tools executed: {', '.join(actions)}."
    return "No action taken."


def _tool_results(messages: list) -> str:
    """Tool output that carries findings, trimmed to keep a supervisor readable.

    Skips the artificial error results the tool-call limit injects; those report
    the budget, not the world.
    """
    parts, seen = [], set()
    for message in messages:
        if getattr(message, "type", None) != "tool":
            continue
        if getattr(message, "status", None) == "error":
            continue
        content = (message.content or "").strip()
        if not content or content in seen:
            continue
        seen.add(content)
        if len(content) > TOOL_RESULT_SALVAGE_CHARS:
            content = content[:TOOL_RESULT_SALVAGE_CHARS] + " ...[truncated]"
        parts.append(f"[{getattr(message, 'name', None) or 'tool'}] {content}")
    return "\n\n".join(parts)


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
