"""Top-level graph: routes between the research and writing teams.

Each team is a compiled sub-graph invoked from a node here.
"""

from typing import Literal

from langchain_core.messages import HumanMessage
from langgraph.graph import START, StateGraph
from langgraph.types import Command

from config import TEAM_RECURSION_LIMIT, TOP_LEVEL_TEAMS
from llm.clients import llm
from logging_config import get_logger
from prompts import TOP_LEVEL_SCOPE
from teams.research import research_graph
from teams.state import State
from teams.supervisor import make_supervisor_node
from teams.visualization import save_graph_image
from teams.writing import writing_graph

logger = get_logger(__name__)

# Sub-graphs inherit the parent's config unless given their own, so a team would
# otherwise draw down the orchestrator's recursion budget and one runaway team
# would starve the other.
TEAM_CONFIG = {"recursion_limit": TEAM_RECURSION_LIMIT}

teams_supervisor_node = make_supervisor_node(
    llm,
    TOP_LEVEL_TEAMS,
    scope=TOP_LEVEL_SCOPE,
)


def call_research_team(state: State) -> Command[Literal["supervisor"]]:
    logger.info(">>> research_team starting (recursion_limit=%d)", TEAM_RECURSION_LIMIT)
    result = research_graph.invoke({"messages": [state["messages"][-1]]}, TEAM_CONFIG)
    logger.info("<<< research_team finished after %d message(s)", len(result["messages"]))
    return Command(
        update={
            "messages": [
                HumanMessage(
                    content=result["messages"][-1].content, name="research_team"
                )
            ]
        },
        goto="supervisor",
    )


def call_writing_team(state: State) -> Command[Literal["supervisor"]]:
    logger.info(">>> writing_team starting (recursion_limit=%d)", TEAM_RECURSION_LIMIT)
    result = writing_graph.invoke({"messages": [state["messages"][-1]]}, TEAM_CONFIG)
    logger.info("<<< writing_team finished after %d message(s)", len(result["messages"]))
    return Command(
        update={
            "messages": [
                HumanMessage(content=result["messages"][-1].content, name="writing_team")
            ]
        },
        goto="supervisor",
    )


def build_super_graph():
    builder = StateGraph(State)
    builder.add_node("supervisor", teams_supervisor_node)
    builder.add_node("research_team", call_research_team)
    builder.add_node("writing_team", call_writing_team)
    builder.add_edge(START, "supervisor")
    return builder.compile()


super_graph = build_super_graph()
save_graph_image(super_graph, "super_graph")
