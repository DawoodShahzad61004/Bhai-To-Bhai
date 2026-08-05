from typing import List, Literal
from state import State, make_supervisor_node, tool_call_limit, agent_report
from langgraph.types import Command
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage 
from tools import scrape_webpages, tavily_tool
from llm_setup import llm

search_agent = create_agent(
    model=llm,
    tools=[tavily_tool],
    name="search_agent",
    system_prompt=(
        "You are a search agent. Search the web and extract "
        "relevant information."
    ),
    middleware=[tool_call_limit()],
)

def search_node(
    state: State,
) -> Command[Literal['supervisor']]:
    result = search_agent.invoke(state)
    return Command(
        update={
            "messages": [HumanMessage(content=agent_report(result), name="search")]
        },
        goto="supervisor"
    )
    
web_scraper_agent = create_agent(
    model=llm,
    tools=[scrape_webpages],
    name="web_scraper",
    system_prompt=(
        "You are a web-scraping agent. Scrape webpages and "
        "extract relevant information."
    ),
    middleware=[tool_call_limit()],
)

def web_scrapper_node(state: State) -> Command[Literal['supervisor']]:
    result = web_scraper_agent.invoke(state)
    return Command(
            update= {
                "messages": [HumanMessage(content=agent_report(result), name="web_scraper")]
            },
            goto="supervisor"
    )
    
research_supervisor_node = make_supervisor_node(
    llm,
    ["search", "web_scraper"],
    scope=(
        "This team only gathers information from the web. It cannot create, "
        "write or save files -- another team does that. Once the information "
        "has been gathered, your work is complete."
    ),
)