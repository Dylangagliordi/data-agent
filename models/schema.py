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
    # "tableau" only when the curated question explicitly names Tableau;
    # default "csv" otherwise — detection never guesses from context alone.
    export_target: Literal["csv", "tableau"] = "csv"

    # Set by build_visualization to the .png file path when chart rendering
    # succeeds; empty string when rendering failed or was not attempted (i.e.
    # for every non-visualization run). Persisted in query_log.jsonl alongside
    # chart_type and output_file_path.
    chart_image_path: str = ""

    # Set by resolve_chart_columns (visualization path only, after execute_sql
    # succeeds) to the REAL result column names the chart should actually plot,
    # resolved by meaning rather than by SQL SELECT-list position — see
    # resolve_chart_columns in agents/sql_analyst.py for why: chart renderers
    # used to pick cols[0]/cols[1] positionally, which silently plotted the wrong
    # metric whenever the column the question cared about wasn't selected first.
    # Left blank (and build_visualization falls back to each renderer's existing
    # positional/numeric-detection logic) when the result is empty/truncated, or
    # when column resolution fails and the deterministic fallback in
    # resolve_chart_columns is used instead (see chart_column_resolution_note).
    chart_category_column: str = ""
    chart_value_column: str = ""
    # Only meaningful for "stacked bar" / "treemap" chart types; empty string
    # otherwise.
    chart_secondary_column: str = ""
    # Non-empty only when resolve_chart_columns had to fall back to its
    # deterministic heuristic instead of a confident LLM pick. Appended to
    # final_answer by build_visualization, same pattern as disclosure_note.
    chart_column_resolution_note: str = ""
    # Set by validate_chart_shape when the chosen chart_type would render
    # broken/misleading against the REAL result (too many pie slices, no real
    # time axis for a line chart, too few points for a histogram, etc.) and had
    # to be overridden to a safer fallback. Empty string when no override was
    # needed. Appended to final_answer by build_visualization.
    chart_type_override_note: str = ""

    # Spec 12 (Scratch Mode): set by check_needs_scratch_mode (LLM judgment,
    # runs after validate_chart_shape) when the question's real intent needs a
    # computed/conditional visual element no fixed chart type can express (a
    # derived threshold, a quadrant split, a conditional highlight) — routes
    # to run_scratch_mode instead of build_visualization. False for every
    # ordinary chart request; the fixed 8-type renderer already handles plain
    # data plots (including basic per-point annotations) without this ever
    # needing to fire.
    needs_scratch_mode: bool = False
    scratch_mode_reasoning: str = ""

    # Populated by surface_transformations (Spec 2, Part B narration source):
    # one entry per real TransformationCandidate that was actually surfaced
    # and decided for THIS question via surface_relevant_transformations /
    # present_transformation_options / the _transformation_decisions cache.
    # Each entry: {"table_name", "candidate" (TransformationCandidate.to_dict()),
    # "chosen_option_id", "reasoning_shown" (the real context/options shown),
    # "fresh" (True if decided during THIS call, False if pulled from the
    # decision cache), "reload_reask" (True only when "fresh" is also True AND
    # this table was actually cleaned/reloaded earlier in this same run —
    # never inferred from cross-session log archaeology)}.
    # utils/narrative.py's build_narrative_walkthrough reads this directly
    # from the logged query_log.jsonl entry — never re-derives it.
    transformation_narrative_log: list = Field(default_factory=list)

    # Populated alongside transformation_narrative_log: every real stored
    # TransformationCandidate for a table touched by this question that
    # surface_relevant_transformations did NOT surface (i.e. existed but
    # wasn't relevant here). Each entry: {"table_name", "candidate"
    # (TransformationCandidate.to_dict())}. Used by build_narrative_walkthrough
    # for the low-emphasis "other optional transformations exist" note.
    transformation_candidates_not_relevant: list = Field(default_factory=list)


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


class ScratchModeSchema(BaseModel):
    """Structured output schema for the check_needs_scratch_mode node only
    (Spec 12). Used via with_structured_output — never exposed to the main
    state directly until its fields are copied into
    needs_scratch_mode/scratch_mode_reasoning.

    Deliberately conservative: needs_scratch_mode should be True only when
    satisfying the question requires computing something and then
    conditionally acting on it (a derived threshold, a quadrant split, a
    conditional highlight, combining more than one piece of information
    visually) — never merely because the question asks for a label,
    annotation, or styling the fixed 8-type renderer already handles.
    """

    needs_scratch_mode: bool
    reasoning: str


class ExplorationHypothesis(BaseModel):
    """Structured output schema for utils/data_cleaning.py's explore_column
    (open-ended, per-column "glance and notice" pass) and _explore_column_pairs
    (cross-column consistency pass) — used via with_structured_output only.

    hypotheses is deliberately loose, plain-English text — never something
    check_rubric() would accept directly as an issue string. Every hypothesis
    returned here MUST be run through _verify_hypothesis (or the mechanical
    column-pair comparison) before it can become a real issue; nothing an LLM
    notices here is trusted on its own. An empty list is a valid, expected
    result — most columns/pairs should produce nothing.
    """

    hypotheses: list[str]


class VerifiedPatternProposal(BaseModel):
    """Structured output schema for utils/data_cleaning.py's _verify_hypothesis
    only — used via with_structured_output. Turns one loose hypothesis into a
    concrete, mechanically-testable claim: a regex to run against the FULL real
    column (never just the sample) and the match fraction it must clear to be
    considered confirmed. The LLM's role stops here; the actual verification
    (does match_frac >= match_threshold against every non-null real value) is
    plain Python/pandas, no LLM involved.
    """

    pattern: str
    match_threshold: float
    description: str


class CategoricalAssignment(BaseModel):
    """One raw_value -> group assignment inside a CategoricalConsolidationProposal."""

    raw_value: str
    group: str


class CategoricalConsolidationProposal(BaseModel):
    """Structured output schema for utils/categorical_consolidation.py's
    _generate_categorical_consolidation_mapping (Spec 3, Part 2) — used via
    with_structured_output only, for the live AI-driven clustering path.

    A list of (raw_value, group) pairs rather than a single dict[str,str]
    field — more reliable for an LLM to produce completely and correctly for
    a large (dozens-of-entries) mapping than one big dict blob. The caller
    mechanically verifies the returned assignments cover every real distinct
    value exactly (no LLM-side guarantee is trusted on its own) before
    accepting the mapping.
    """

    assignments: list[CategoricalAssignment]
