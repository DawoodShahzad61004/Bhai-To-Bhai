from typing import List, Literal
from typing_extensions import TypedDict
from llm_caller import llm_invoke
from llm_setup import llm
from tools import *
from langchain.agents.middleware import ToolCallLimitMiddleware
from langgraph.graph import MessagesState, START, END
from langgraph.types import Command

class State(MessagesState):
    next: str


# llama-3.1-8b on TGI is grammar-constrained to emit a tool call on every turn
# while tools are attached, so it never produces a final answer and the ReAct
# loop runs until the recursion limit. Bound each worker instead.
def tool_call_limit(run_limit: int = 3) -> ToolCallLimitMiddleware:
    return ToolCallLimitMiddleware(run_limit=run_limit, exit_behavior="end")


def agent_report(result: dict) -> str:
    """Describe what a worker actually did.

    When the tool-call limit ends a run, the final message is a bookkeeping stub
    ("Tool call limit reached: ..."). Handing that to the supervisor reads as
    unfinished work and it routes straight back, so report the tool calls
    instead -- that is the part the supervisor can actually act on.
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

def make_supervisor_node(llm, members: List[str], scope: str = "", max_worker_reports: int = 2):
    options = ["FINISH"] + members
    system_prompt = (
        f"You are a supervisor agent tasked with managing a conversation between the "
        f"following workers: {members}. Given the following user request, "
        "respond with the worker to act next. Each worker will perform a "
        "task and respond with their results and status. When finished, "
        "respond with 'FINISH'."
    )
    # A team is handed the whole user request, including parts its workers have
    # no tools for. Without a stated remit the supervisor re-routes forever
    # chasing work this team structurally cannot do.
    if scope:
        system_prompt += f" {scope}"
    system_prompt += (
        " Respond with FINISH as soon as this team's part of the request is done,"
        " or if no worker here can make further progress. Never route to a worker"
        " that has already reported the result you need."
    )
        
    class Router(TypedDict):
        """Worker to route to next. Route to FINISH when no more work is needed."""
        next: Literal[*options]

    # TGI 3.0 returns HTTP 500 for response_format json_schema/json_object
    # (langchain-openai's default), but handles tool calling correctly.
    router_llm = llm.with_structured_output(Router, method="function_calling")

    def supervisor(state: State) -> Command[Literal[*members, END]]:
        messages = [
            {"role": "system", "content": system_prompt}
        ] + state["messages"]

        result = llm_invoke(router_llm, messages, caller_tag="supervisor")
        if not result.ok:
            raise RuntimeError(
                f"Supervisor routing call failed ({result.error_kind}): {result.error_message}"
            )
        goto = result.response["next"]
        if goto == "FINISH":
            return Command(goto=END)

        # The router re-selects a worker that has already reported, so a request
        # this team cannot satisfy loops until the recursion limit. Telling it so
        # in the system prompt is not enough -- cap the repeats structurally.
        reports = sum(1 for m in state["messages"] if getattr(m, "name", None) == goto)
        if reports >= max_worker_reports:
            return Command(goto=END)

        return Command(
            goto=goto,
            update={"next":goto},
        )

    return supervisor