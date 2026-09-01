"""
Pydantic schemas for the SQL analyst sub-agent's graph state.
"""

from typing import Annotated, Literal, Optional

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

    # Auto-clean redirect fields: set by add_context after the data-quality status
    # check; consumed by the conditional edge and clean_and_reload node.
    #
    # data_quality_action: "needs_cleaning" when at least one queried table has
    #   status == "fail" AND a non-null source_folder AND hasn't been attempted yet
    #   this question; "proceed" otherwise.
    # tables_to_clean: list of {"table": <name>, "source_folder": <path>} dicts
    #   for every table that triggered "needs_cleaning" this pass.
    # cleaning_attempted_tables: list of table names already cleaned (or attempted)
    #   this question — the stop condition that prevents the redirect from looping.
    data_quality_action: Literal["proceed", "needs_cleaning"] = "proceed"
    tables_to_clean: list = Field(default_factory=list)
    cleaning_attempted_tables: list = Field(default_factory=list)

    # Visualization fields: set when the router dispatches to visualize_node
    # instead of sql_node. wants_visualization=False leaves every visualization
    # node unreachable — the routing functions gate on this flag so normal
    # sql_analyst questions are completely unaffected.
    wants_visualization: bool = False
    chart_type: str = ""
    chart_type_source: Literal["explicit", "reasoned"] = "explicit"
    chart_type_reasoning: str = ""
    output_file_path: str = ""


class JudgeSchema(BaseModel):
    """Structured output schema for the safety-judge node only.

    Used via with_structured_output — never exposed to the main state directly
    until its fields are copied into is_safe/comments.
    """

    answer: Literal["yes", "no"]
    comments: str


class ChartTypeSchema(BaseModel):
    """Structured output schema for the determine_chart_type node only.

    Used via with_structured_output — never exposed to the main state directly
    until its fields are copied into chart_type/chart_type_source/chart_type_reasoning.
    chart_type_reasoning must be a real, specific justification when
    chart_type_source is "reasoned"; it must be empty string when "explicit".
    """

    chart_type: str
    chart_type_source: Literal["explicit", "reasoned"]
    chart_type_reasoning: str
