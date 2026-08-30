"""
Pydantic schema for the ETL analyst sub-agent's graph state.

Standard ReAct pattern: the only real state is the message history — the LLM reasons
over it directly to decide which tool (if any) to call next, and LangGraph's prebuilt
ToolNode + tools_condition handle routing based on tool_calls attached to the last
AIMessage. There's no other cross-node state to track here (unlike the SQL analyst,
which threads curated_question/generated_sql_query/etc. between fixed-purpose nodes) —
ReAct's loop doesn't need named intermediate fields, just the growing conversation.
"""

from typing import Annotated

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class ETLAnalystState(BaseModel):
    """Shared state threaded through every node of the ETL analyst's ReAct graph."""

    messages: Annotated[list, add_messages] = Field(default_factory=list)
