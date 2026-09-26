"""
Pydantic schema for the Conductor's graph state (Spec 16).

Same standard ReAct pattern as models/etl_schema.py:ETLAnalystState — the
only real state is the growing message history; LangGraph's prebuilt
ToolNode + tools_condition handle routing based on tool_calls attached to
the last AIMessage. No named intermediate fields needed.
"""

from typing import Annotated

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class ConductorState(BaseModel):
    """Shared state threaded through every node of the Conductor's ReAct graph."""

    messages: Annotated[list, add_messages] = Field(default_factory=list)
