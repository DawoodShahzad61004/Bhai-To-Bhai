"""Writing team: turns information already in the conversation into documents.

This team has no web tools -- gathering information is the research team's job.
"""

from typing import Literal

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langgraph.graph import START, StateGraph
from langgraph.types import Command

from config import WRITING_TEAM_MEMBERS
from llm.clients import llm
from logging_config import get_logger
from prompts import (
    CHART_GENERATOR_AGENT,
    DOC_WRITER_AGENT,
    NOTE_TAKER_AGENT,
    WRITING_TEAM_SCOPE,
)
from teams.state import State
from teams.supervisor import agent_report, make_supervisor_node, tool_call_limit
from teams.visualization import save_graph_image
from tools import (
    create_outline,
    edit_document,
    python_repl_tool,
    read_document,
    write_document,
)

logger = get_logger(__name__)

doc_writer_agent = create_agent(
    model=llm,
    tools=[write_document, edit_document, read_document],
    name="doc_writer_agent",
    system_prompt=DOC_WRITER_AGENT,
    middleware=[tool_call_limit()],
)


def doc_writing_node(state: State) -> Command[Literal["supervisor"]]:
    logger.info("--> worker 'doc_writer' starting")
    result = doc_writer_agent.invoke(state)
    report = agent_report(result)
    logger.info("<-- worker 'doc_writer' reported: %s", report[:200])
    return Command(
        update={
            "messages": [HumanMessage(content=report, name="doc_writer")]
        },
        goto="supervisor",
    )


note_taking_agent = create_agent(
    model=llm,
    tools=[create_outline, read_document],
    name="note_taking_agent",
    system_prompt=NOTE_TAKER_AGENT,
    middleware=[tool_call_limit()],
)


def note_taking_node(state: State) -> Command[Literal["supervisor"]]:
    logger.info("--> worker 'note_taker' starting")
    result = note_taking_agent.invoke(state)
    report = agent_report(result)
    logger.info("<-- worker 'note_taker' reported: %s", report[:200])
    return Command(
        update={
            "messages": [HumanMessage(content=report, name="note_taker")]
        },
        goto="supervisor",
    )


chart_generating_agent = create_agent(
    model=llm,
    tools=[read_document, python_repl_tool],
    name="chart_generating_agent",
    system_prompt=CHART_GENERATOR_AGENT,
    middleware=[tool_call_limit()],
)


def chart_generating_node(state: State) -> Command[Literal["supervisor"]]:
    logger.info("--> worker 'chart_generator' starting")
    result = chart_generating_agent.invoke(state)
    report = agent_report(result)
    logger.info("<-- worker 'chart_generator' reported: %s", report[:200])
    return Command(
        update={
            "messages": [
                HumanMessage(content=report, name="chart_generator")
            ]
        },
        goto="supervisor",
    )


writing_supervisor_node = make_supervisor_node(
    llm,
    WRITING_TEAM_MEMBERS,
    scope=WRITING_TEAM_SCOPE,
)


def build_writing_graph():
    builder = StateGraph(State)
    builder.add_node("supervisor", writing_supervisor_node)
    builder.add_node("doc_writer", doc_writing_node)
    builder.add_node("note_taker", note_taking_node)
    builder.add_node("chart_generator", chart_generating_node)
    builder.add_edge(START, "supervisor")
    return builder.compile()


writing_graph = build_writing_graph()
save_graph_image(writing_graph, "writing_graph")
