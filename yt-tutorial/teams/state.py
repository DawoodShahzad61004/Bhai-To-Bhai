"""Shared graph state.

Every team and the orchestrator use this same schema, so a worker's report is
visible to whichever supervisor reads the transcript next.
"""

from langgraph.graph import MessagesState


class State(MessagesState):
    """Conversation history plus the supervisor's most recent routing choice."""

    next: str
