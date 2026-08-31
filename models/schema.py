"""
Pydantic schemas for the SQL analyst sub-agent's graph state.
"""

from typing import Annotated, Literal

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field


class SQLAnalystState(BaseModel):
    """Shared state threaded through every node of the SQL analyst graph.

    Every field has a real default so the graph can be invoked with a minimal
    or partial starting state (e.g. just {"user_question": "..."}) without a
    Pydantic validation error.
    """

    messages: Annotated[list, add_messages] = Field(default_factory=list)
    user_question: str = ""
    curated_question: str = ""
    prompt_query_context: str = ""
    generated_sql_query: str = ""
    is_safe: Literal["yes", "no"] = "no"
    comments: str = ""
    sql_query_execution_result: str = ""
    final_answer: str = ""

    # Populated by add_context from the persistent _data_quality_status table: one
    # entry per table that has either NO recorded data-quality check at all, or a
    # "fail"-level unresolved issue (never "warn" — warn-level status is real but not
    # serious enough to surface in generate_sql's context OR the final answer; see
    # add_context / represent_final_answer). Each entry is {"table": ..., "warning": ...}.
    # represent_final_answer filters this down to only tables the actually-generated
    # SQL query touches before deciding whether to mention anything.
    data_quality_warnings: list = Field(default_factory=list)

    # Not in the original field list from the spec, but required to implement the
    # "cap at 5 total attempts across the whole generate->execute cycle" rule —
    # there is no other way to count retries across graph steps without it.
    sql_attempts: int = 0


class JudgeSchema(BaseModel):
    """Structured output schema for the safety-judge node only.

    Used via with_structured_output — never exposed to the main state directly
    until its fields are copied into is_safe/comments.
    """

    answer: Literal["yes", "no"]
    comments: str
