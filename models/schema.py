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


class JudgeSchema(BaseModel):
    """Structured output schema for the safety-judge node only.

    Used via with_structured_output — never exposed to the main state directly
    until its fields are copied into is_safe/comments.
    """

    answer: Literal["yes", "no"]
    comments: str
