from typing import List, Literal
from state import State
from writing_team import chart_generating_node, doc_writing_node, doc_writing_supervisor_node, note_taking_node
from langgraph.graph import StateGraph, START
from pathlib import Path

writing_builder = StateGraph(State)
writing_builder.add_node("supervisor", doc_writing_supervisor_node)
writing_builder.add_node("doc_writer", doc_writing_node)
writing_builder.add_node("note_taker", note_taking_node)
writing_builder.add_node("chart_generator", chart_generating_node)

writing_builder.add_edge(START, "supervisor") 
writing_graph = writing_builder.compile()

try:
    png_data = writing_graph.get_graph().draw_mermaid_png()
    Path("writing_graph.png").write_bytes(png_data)
except ImportError:
    print("Error in graph image generation. Please install the required dependencies for graph visualization.")
    
