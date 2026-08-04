from typing import List, Literal
from state import State, make_supervisor_node
from search_node import search_node, web_scrapper_node, research_supervisor_node
from langgraph.graph import MessagesState, StateGraph, START, END
from pathlib import Path

research_builder = StateGraph(State)
research_builder.add_node("supervisor", research_supervisor_node)
research_builder.add_node("search", search_node)
research_builder.add_node("web_scraper", web_scrapper_node)

research_builder.add_edge(START, "supervisor") 
research_graph = research_builder.compile()

try:
    png_data = research_graph.get_graph().draw_mermaid_png()
    Path("research_graph.png").write_bytes(png_data)
except ImportError:
    print("Error in graph image generation. Please install the required dependencies for graph visualization.")