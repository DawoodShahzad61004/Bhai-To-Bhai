from typing import List, Literal
from state import State, make_supervisor_node, tool_call_limit, agent_report
from langgraph.types import Command
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from tools import create_outline, write_document, edit_document, read_document, python_repl_tool
from llm_setup import llm

doc_writer_agent = create_agent(
    model=llm,
    tools=[write_document, edit_document, read_document],
    name="doc_writer_agent",
    system_prompt=(
        "You can read, write and edit documents based on taker's outlines. "
        "Don't ask follow up questions."
    ),
    middleware=[tool_call_limit()],
)

def doc_writing_node(state: State) -> Command[Literal['supervisor']]:
    result = doc_writer_agent.invoke(state)
    return Command(
        update={
            "messages": [HumanMessage(content=agent_report(result), name="doc_writer")]
        },
        goto="supervisor"
    )
    
note_taking_agent = create_agent(
    model=llm,
    tools=[create_outline, read_document],
    system_prompt=(
        "You can read documents and create outlines for the document writer. "
        "Don't ask follow up questions."
    ),
    middleware=[tool_call_limit()],
)
    
def note_taking_node(state: State) -> Command[Literal['supervisor']]:
    result = note_taking_agent.invoke(state)
    return Command(
        update={
            "messages": [HumanMessage(content=agent_report(result), name="note_taker")]
        },
        goto="supervisor"
)
    
chart_generating_agent = create_agent(
    model=llm,
    tools=[read_document, python_repl_tool],
    # system_prompt=(
    #     "You can read documents and generate charts based on the document content. "
    #     "Don't ask follow up questions."
    # ),
    middleware=[tool_call_limit()],
)
    
def chart_generating_node(state: State) -> Command[Literal['supervisor']]:
    result = chart_generating_agent.invoke(state)
    return Command(
        update={
            "messages": [HumanMessage(content=agent_report(result), name="chart_generator")]
        },
        goto="supervisor"
)
    
    
doc_writing_supervisor_node = make_supervisor_node(
    llm,
    ["doc_writer", "note_taker", "chart_generator"],
    scope=(
        "This team writes and edits documents from information already provided "
        "in the conversation. It cannot search the web -- another team does that."
    ),
)