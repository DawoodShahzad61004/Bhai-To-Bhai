"""Workers that ask Claude Code to do the job, instead of doing it themselves.

Each one is the Claude Code counterpart of a worker in teams/: same node name,
same signature, same report back to the same supervisor. What changes is the
middle -- where teams/ builds a LangChain agent loop around a model, this builds
a prompt, runs the CLI, and files whatever came back.

There is no reasoning here on purpose. The brief a worker sends is the brief its
LangChain counterpart already runs on, imported unchanged from prompts.py; the
only thing this module adds is which tools that brief's names point at in this
run, and the logging around the call.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from claude_agents import prompts as framing
from claude_agents.cli import mcp_tool_name, run_claude
from claude_agents.mcp_bridge import mcp_config_json
from config import CLAUDE_CODE_SEND_CUSTOM_TOOLS, WORKSPACE_DIR
from logging_config import get_logger
from prompts import (
    CHART_GENERATOR_AGENT,
    DOC_WRITER_AGENT,
    NOTE_TAKER_AGENT,
    SEARCH_AGENT,
)
from teams.state import State

logger = get_logger(__name__)


@dataclass(frozen=True)
class ClaudeAgent:
    """One worker: which brief it runs on, and what it runs it with."""

    name: str
    # Imported verbatim from prompts.py -- the same text the LangChain worker gets.
    role: str
    # Tools from tools/, attached over MCP when CLAUDE_CODE_SEND_CUSTOM_TOOLS is on.
    custom_tools: tuple[str, ...]
    # Claude Code's own tools, used instead when it is off.
    builtin_tools: tuple[str, ...]
    # What the brief's tool names mean once those built-ins are what it has.
    builtin_mapping: str


CLAUDE_AGENTS: dict[str, ClaudeAgent] = {
    "search": ClaudeAgent(
        name="search",
        role=SEARCH_AGENT,
        custom_tools=("tavily_search",),
        builtin_tools=("WebSearch", "WebFetch"),
        builtin_mapping=(
            "Where the brief says tavily_search, use WebSearch. WebFetch reads a "
            "page you already have the URL of. Everything the brief says about "
            "citing the URL a fact came from still applies -- more so, because "
            "these tools return more than a search snippet and it is easier to "
            "drift into what you already knew."
        ),
    ),
    "doc_writer": ClaudeAgent(
        name="doc_writer",
        role=DOC_WRITER_AGENT,
        custom_tools=("write_document", "edit_document", "read_document"),
        builtin_tools=("Read", "Write", "Edit"),
        builtin_mapping=(
            "Where the brief says write_document, use Write; edit_document, use "
            "Edit; read_document, use Read. Write replaces the whole file exactly "
            "as write_document does, so the brief's rule holds: compose the "
            "document in full, then write it once. Use the bare file name the "
            "request asked for -- you are already in the workspace directory."
        ),
    ),
    "note_taker": ClaudeAgent(
        name="note_taker",
        role=NOTE_TAKER_AGENT,
        custom_tools=("create_outline", "read_document"),
        builtin_tools=("Read", "Write"),
        builtin_mapping=(
            "There is no create_outline here. Use Write to save outline.txt in "
            "the workspace directory, numbering the points yourself -- '1. ', "
            "'2. ' and so on, one per line -- which is what create_outline does. "
            "outline.txt is still the only name you may use; the file named in "
            "the request belongs to the document writer."
        ),
    ),
    "chart_generator": ClaudeAgent(
        name="chart_generator",
        role=CHART_GENERATOR_AGENT,
        custom_tools=("read_document", "python_repl_tool"),
        builtin_tools=("Read", "Write", "Bash"),
        builtin_mapping=(
            "Where the brief says read_document, use Read. There is no Python "
            "REPL: write the matplotlib script to a file with Write and run it "
            "with Bash, using the interpreter at {python}. Save the figure into "
            "the workspace directory, which is where you already are."
        ),
    ),
}


def _tools_note(agent: ClaudeAgent) -> str:
    """Tell the brief's tool names apart from the tools actually attached."""
    if CLAUDE_CODE_SEND_CUSTOM_TOOLS:
        return framing.CUSTOM_TOOLS_NOTE.format(
            tool_list=", ".join(mcp_tool_name(t) for t in agent.custom_tools)
        )
    return framing.BUILTIN_TOOLS_NOTE.format(
        tool_list=", ".join(agent.builtin_tools),
        mapping=agent.builtin_mapping.replace("{python}", sys.executable),
    )


def run_claude_worker(agent: ClaudeAgent, state: State) -> str:
    """Run one worker turn and always come back with a report.

    Same contract as teams/supervisor.py's run_worker, and for the same reason:
    a supervisor can route around a worker that failed, but not around a
    traceback -- and by the time a worker fails, other teams have already done
    work that a raised exception would throw away.
    """
    logger.info("--> claude worker '%s' starting", agent.name)
    result = run_claude(
        framing.WORKER_REQUEST.format(
            transcript=framing.transcript(state["messages"]), name=agent.name
        ),
        system_prompt=framing.WORKER_FRAME.format(
            workspace_note=framing.WORKSPACE_NOTE.format(workspace=WORKSPACE_DIR),
            tools_note=_tools_note(agent),
            role=agent.role,
        ),
        tag=f"worker-{agent.name}",
        builtin_tools=() if CLAUDE_CODE_SEND_CUSTOM_TOOLS else agent.builtin_tools,
        mcp_tools=agent.custom_tools if CLAUDE_CODE_SEND_CUSTOM_TOOLS else (),
        mcp_config=mcp_config_json(agent.custom_tools) if CLAUDE_CODE_SEND_CUSTOM_TOOLS else "",
    )

    if not result.ok:
        logger.error("Claude worker '%s' produced no report: %s", agent.name, result.error_kind)
        return (
            f"Worker '{agent.name}' failed ({result.error_kind}) and produced no "
            f"report. Anything it saved before failing is still on disk. "
            f"Detail: {result.error_message[:300]}"
        )
    if not result.text:
        logger.warning("Claude worker '%s' returned an empty reply", agent.name)
        return f"Worker '{agent.name}' returned an empty report."

    logger.info("<-- claude worker '%s' reported: %s", agent.name, result.text[:200])
    return result.text


def make_claude_worker_node(name: str):
    """A graph node for `name`, interchangeable with the one in teams/."""
    agent = CLAUDE_AGENTS[name]

    def node(state: State) -> Command[Literal["supervisor"]]:
        report = run_claude_worker(agent, state)
        return Command(
            update={"messages": [HumanMessage(content=report, name=agent.name)]},
            goto="supervisor",
        )

    node.__name__ = f"claude_{name}_node"
    return node
