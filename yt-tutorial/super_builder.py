from pathlib import Path
from typing import List, Literal
from state import State, make_supervisor_node
from langgraph.graph import StateGraph, START
from langgraph.types import Command
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage 
from writing_builder import writing_graph
from research_builder import research_graph
from llm_setup import llm

teams_supervisor_node = make_supervisor_node(
    llm,
    ["research_team", "writing_team"],
)

# Sub-graphs otherwise inherit the parent's config, so a team draws down the
# super-graph's recursion budget and one runaway team starves the other.
TEAM_CONFIG = {"recursion_limit": 25}

def call_research_team(state: State) -> Command[Literal['supervisor']]:
    result = research_graph.invoke({"messages": [state["messages"][-1]]}, TEAM_CONFIG)
    return Command(
        update={
            "messages": [HumanMessage(content=result["messages"][-1].content, name="research_team")]
        },
        goto="supervisor"
    )

def call_writing_team(state: State) -> Command[Literal['supervisor']]:
    result = writing_graph.invoke({"messages": [state["messages"][-1]]}, TEAM_CONFIG)
    return Command(
        update={
            "messages": [HumanMessage(content=result["messages"][-1].content, name="writing_team")]
        },
        goto="supervisor"
    )

super_builder = StateGraph(State)
super_builder.add_node("supervisor", teams_supervisor_node)
super_builder.add_node("research_team", call_research_team)
super_builder.add_node("writing_team", call_writing_team)

super_builder.add_edge(START, "supervisor")

super_graph = super_builder.compile()

try:
    png_data = super_graph.get_graph().draw_mermaid_png()
    Path("super_graph.png").write_bytes(png_data)
except ImportError:
    print("Error in graph image generation. Please install the required dependencies for graph visualization.")
    
