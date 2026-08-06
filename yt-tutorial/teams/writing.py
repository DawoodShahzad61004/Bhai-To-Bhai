"""Writing team: turns information already in the conversation into documents.

This team has no web tools -- gathering information is the research team's job.

config.USE_CLAUDE_CODE_AGENTS swaps the supervisor and all three workers for
their Claude Code counterparts in claude_agents/. The graph below does not
change either way; only what sits behind each node does.
"""

from typing import Literal

from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langgraph.graph import START, StateGraph
from langgraph.types import Command

from claude_agents import make_claude_supervisor_node, make_claude_worker_node
from config import USE_CLAUDE_CODE_AGENTS, WRITING_TEAM_MEMBERS
from llm.clients import llm
from logging_config import get_logger
from prompts import (
    CHART_GENERATOR_AGENT,
    DOC_WRITER_AGENT,
    NOTE_TAKER_AGENT,
    WRITING_TEAM_SCOPE,
)
from teams.state import State
from teams.supervisor import (
    make_supervisor_node,
    run_worker,
    single_call_limit,
    tool_call_limit,
)
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
    middleware=[tool_call_limit(), single_call_limit("write_document")],
)


def langchain_doc_writing_node(state: State) -> Command[Literal["supervisor"]]:
    report = run_worker(doc_writer_agent, "doc_writer", state)
    return Command(
        update={
            "messages": [HumanMessage(content=report, name="doc_writer")]
        },
        goto="supervisor",
    )


doc_writing_node = (
    make_claude_worker_node("doc_writer")
    if USE_CLAUDE_CODE_AGENTS
    else langchain_doc_writing_node
)


note_taking_agent = create_agent(
    model=llm,
    tools=[create_outline, read_document],
    name="note_taking_agent",
    system_prompt=NOTE_TAKER_AGENT,
    middleware=[tool_call_limit(), single_call_limit("create_outline")],
)


def langchain_note_taking_node(state: State) -> Command[Literal["supervisor"]]:
    report = run_worker(note_taking_agent, "note_taker", state)
    return Command(
        update={
            "messages": [HumanMessage(content=report, name="note_taker")]
        },
        goto="supervisor",
    )


note_taking_node = (
    make_claude_worker_node("note_taker")
    if USE_CLAUDE_CODE_AGENTS
    else langchain_note_taking_node
)


chart_generating_agent = create_agent(
    model=llm,
    tools=[read_document, python_repl_tool],
    name="chart_generating_agent",
    system_prompt=CHART_GENERATOR_AGENT,
    middleware=[tool_call_limit()],
)


def langchain_chart_generating_node(state: State) -> Command[Literal["supervisor"]]:
    report = run_worker(chart_generating_agent, "chart_generator", state)
    return Command(
        update={
            "messages": [
                HumanMessage(content=report, name="chart_generator")
            ]
        },
        goto="supervisor",
    )


chart_generating_node = (
    make_claude_worker_node("chart_generator")
    if USE_CLAUDE_CODE_AGENTS
    else langchain_chart_generating_node
)


writing_supervisor_node = (
    make_claude_supervisor_node(WRITING_TEAM_MEMBERS, scope=WRITING_TEAM_SCOPE)
    if USE_CLAUDE_CODE_AGENTS
    else make_supervisor_node(llm, WRITING_TEAM_MEMBERS, scope=WRITING_TEAM_SCOPE)
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
