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


def team_report(result: dict, sent: int) -> str:
    """Everything a team produced this turn, not just its closing message.

    A team's last message is whichever worker happened to report last, which
    after a forced FINISH is often the thinnest one. Collapsing to it throws
    away findings the team really did produce -- and those findings are the only
    thing the next team has to work from.
    """
    parts, seen = [], set()
    for message in result["messages"][sent:]:
        text = (message.content or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        name = getattr(message, "name", None)
        parts.append(f"[{name}] {text}" if name else text)
    return "\n\n".join(parts) or "No output produced."


def _call_team(graph, name: str, state: State) -> Command[Literal["supervisor"]]:
    """Run a team over the whole conversation and fold back everything it added.

    The full history goes in, not just the last message: a team invoked a second
    time would otherwise be handed its own previous report and research itself,
    and the writing team would never see the original request -- including the
    file name it is supposed to produce.
    """
    logger.info(">>> %s starting (recursion_limit=%d)", name, TEAM_RECURSION_LIMIT)
    sent = state["messages"]
    result = graph.invoke({"messages": sent}, TEAM_CONFIG)
    report = team_report(result, len(sent))
    logger.info(
        "<<< %s finished after %d new message(s)", name, len(result["messages"]) - len(sent)
    )
    return Command(
        update={"messages": [HumanMessage(content=report, name=name)]},
        goto="supervisor",
    )


def call_research_team(state: State) -> Command[Literal["supervisor"]]:
    return _call_team(research_graph, "research_team", state)


def call_writing_team(state: State) -> Command[Literal["supervisor"]]:
    return _call_team(writing_graph, "writing_team", state)


def build_super_graph():
    builder = StateGraph(State)
    builder.add_node("supervisor", teams_supervisor_node)
    builder.add_node("research_team", call_research_team)
    builder.add_node("writing_team", call_writing_team)
    builder.add_edge(START, "supervisor")
    return builder.compile()


super_graph = build_super_graph()
save_graph_image(super_graph, "super_graph")
