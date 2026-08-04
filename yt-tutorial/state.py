from typing import List, Literal
from llm_caller import llm_invoke
from llm_setup import llm
from tools import *
from langgraph.graph import MessagesState, START, END
from langgraph.types import Command

class State(MessagesState):
    pass

def make_supervisor_node(llm, members: List[str]) -> str:
    options = ["FINISH"] + members
    system_prompt = (
        f"You are a supervisor agent tasked with managing a conversation between the"
        f"following workers: {members}. Given the following user request, "
        "respond with the worker to act next. Each worker will perform a "
        "task and respond with their results and status. When finished, "
        "respond with 'FINISH'."
    )
        
    def supervisor(state: State) -> Command[Literal[*members, END]]:
        messages = [
            {"role": "system", "content": system_prompt}
        ] + state["messages"]
        
        response = llm_invoke(llm, messages)
        goto = response["next"]
        if goto == "FINISH":
            return Command(goto=END)
        return Command(
            goto=goto, 
            update={"next":goto},
        )
    
    return supervisor