"""
Pydantic schemas for the top-level data_agent router graph.

This is a separate, thin state layer sitting ABOVE the SQL analyst and ETL
analyst sub-agents: the router only classifies an incoming message and
dispatches to whichever sub-agent's own (already compiled, already tested)
graph should handle it. It does not duplicate either sub-agent's internal
state — sql_node/etl_node build fresh sub-agent state on the way in and only
copy the sub-agent's own final answer back out onto this schema's own
final_answer field.
"""

from typing import Annotated, Literal

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class DataAgentSchema(BaseModel):
    """Shared state threaded through the router graph's nodes.

    route_response holds ONLY the router's classification ("sql_analyst" or
    "etl_analyst") and is never overwritten after the router node sets it —
    that is what keeps the original classification inspectable even after a
    sub-agent has since run and populated final_answer.
    """

    messages: Annotated[list, add_messages] = Field(default_factory=list)
    route_response: str = ""
    route_comments: str = ""
    final_answer: str = ""


class RouterSchema(BaseModel):
    """Structured-output schema for the router node only (used via
    with_structured_output) — never exposed to the main state directly until
    its fields are copied into route_response/route_comments."""

    answer: Literal["sql_analyst", "etl_analyst"]
    comments: str
