"""Research team: gathers information from the web.

Workers report back to their supervisor, which routes among them until the
information is gathered. This team has no file tools -- producing documents is
the writing team's job.
"""

from typing import Literal

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langgraph.graph import START, StateGraph
from langgraph.types import Command

from config import RESEARCH_TEAM_MEMBERS
from llm.clients import llm
from logging_config import get_logger
from prompts import RESEARCH_TEAM_SCOPE, SEARCH_AGENT, WEB_SCRAPER_AGENT
from teams.state import State
from teams.supervisor import agent_report, make_supervisor_node, tool_call_limit
from teams.visualization import save_graph_image
from tools import scrape_webpages, tavily_tool

logger = get_logger(__name__)

search_agent = create_agent(
    model=llm,
    tools=[tavily_tool],
    name="search_agent",
    system_prompt=SEARCH_AGENT,
    middleware=[tool_call_limit()],
)


def search_node(state: State) -> Command[Literal["supervisor"]]:
    logger.info("--> worker 'search' starting")
    result = search_agent.invoke(state)
    report = agent_report(result)
    logger.info("<-- worker 'search' reported: %s", report[:200])
    return Command(
        update={"messages": [HumanMessage(content=report, name="search")]},
        goto="supervisor",
    )


web_scraper_agent = create_agent(
    model=llm,
    tools=[scrape_webpages],
    name="web_scraper",
    system_prompt=WEB_SCRAPER_AGENT,
    middleware=[tool_call_limit()],
)


def web_scraper_node(state: State) -> Command[Literal["supervisor"]]:
    logger.info("--> worker 'web_scraper' starting")
    result = web_scraper_agent.invoke(state)
    report = agent_report(result)
    logger.info("<-- worker 'web_scraper' reported: %s", report[:200])
    return Command(
        update={
            "messages": [HumanMessage(content=report, name="web_scraper")]
        },
        goto="supervisor",
    )


research_supervisor_node = make_supervisor_node(
    llm,
    RESEARCH_TEAM_MEMBERS,
    scope=RESEARCH_TEAM_SCOPE,
)


def build_research_graph():
    builder = StateGraph(State)
    builder.add_node("supervisor", research_supervisor_node)
    builder.add_node("search", search_node)
    builder.add_node("web_scraper", web_scraper_node)
    builder.add_edge(START, "supervisor")
    return builder.compile()


research_graph = build_research_graph()
save_graph_image(research_graph, "research_graph")
