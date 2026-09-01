"""Standalone test for the determine_chart_type node.

Two cases:
1. Question with an explicit chart type named -> chart_type_source="explicit",
   chart_type_reasoning is empty string.
2. Genuinely ambiguous question with no chart type named -> chart_type_source="reasoned",
   chart_type_reasoning is a real, specific non-empty string.
"""

from agents.sql_analyst import determine_chart_type
from models.schema import SQLAnalystState

print("=" * 70)
print("CASE 1: explicit chart type named in question")
print("=" * 70)
explicit_state = SQLAnalystState(
    wants_visualization=True,
    curated_question="Show me a bar chart of total revenue by product category.",
)
result1 = determine_chart_type(explicit_state)
print("chart_type:", result1["chart_type"])
print("chart_type_source:", result1["chart_type_source"])
print("chart_type_reasoning:", repr(result1["chart_type_reasoning"]))

assert result1["chart_type"], "chart_type must not be empty"
assert result1["chart_type_source"] == "explicit", (
    f"expected 'explicit' for an explicitly named chart type, got {result1['chart_type_source']!r}"
)
assert result1["chart_type_reasoning"] == "", (
    f"chart_type_reasoning must be empty string for explicit source, got {result1['chart_type_reasoning']!r}"
)
print("PASS: explicit chart type -> source='explicit', reasoning empty.\n")

print("=" * 70)
print("CASE 2: no chart type named -> must reason toward a fit")
print("=" * 70)
ambiguous_state = SQLAnalystState(
    wants_visualization=True,
    curated_question=(
        "What are monthly order counts over the past year, broken down by month?"
    ),
)
result2 = determine_chart_type(ambiguous_state)
print("chart_type:", result2["chart_type"])
print("chart_type_source:", result2["chart_type_source"])
print("chart_type_reasoning:", result2["chart_type_reasoning"])

assert result2["chart_type"], "chart_type must not be empty"
assert result2["chart_type_source"] == "reasoned", (
    f"expected 'reasoned' when no chart type is named, got {result2['chart_type_source']!r}"
)
assert result2["chart_type_reasoning"], (
    "chart_type_reasoning must be a real, non-empty justification when source is 'reasoned'"
)
assert result2["chart_type_reasoning"] not in ("best fit", "seems appropriate", "most appropriate"), (
    f"chart_type_reasoning must be specific, not a generic placeholder: {result2['chart_type_reasoning']!r}"
)
print("PASS: no explicit chart type -> source='reasoned', real specific reasoning produced.\n")

print("=" * 70)
print("ALL DETERMINE_CHART_TYPE ASSERTIONS PASSED")
print("=" * 70)
