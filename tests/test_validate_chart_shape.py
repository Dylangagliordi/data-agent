"""
Unit tests (no LLM, no DB) for validate_chart_shape: determine_chart_type picks
the chart type BEFORE generate_sql runs, purely from question wording — some of
its own rules (e.g. "pie chart only for <=5 categories") are unenforceable at
that point since the real result doesn't exist yet. validate_chart_shape closes
that gap by checking the REAL parsed result against state.chart_type, pure
Python, and overriding to a safer chart type when it would render broken or
misleading.

Test 1: a vague-chart-language question whose real result has 12+ categories
must never render as pie — downgrades to bar.

Test 2: a "trend"/"over time" question whose real result's category column
turns out non-temporal (plain category strings, not dates) downgrades from
line to bar, with a visible chart_type_override_note.

Test 3: explicitly requesting "pie chart" for a 15-category result returns a
bar chart, with an explicit disclosure naming what was asked for, what was
rendered instead, and why (chart_type_source == "explicit" path).

Test 4: a compliant chart (<=5 categories, real pie) is left untouched — no
false-positive override.

Test 5: scatter downgrades to bar when the result has fewer than 2 real
numeric measures (excluding id/count columns).

Test 6: stacked bar downgrades to plain bar when fewer than 3 columns are
present.

Test 7: histogram downgrades to bar when too few rows are returned.
"""

import json

from agents.sql_analyst import validate_chart_shape
from models.schema import SQLAnalystState


def _state(chart_type, source, columns, rows, category_col="", question="Q"):
    return SQLAnalystState(
        wants_visualization=True,
        curated_question=question,
        chart_type=chart_type,
        chart_type_source=source,
        sql_query_execution_result=json.dumps(
            {"columns": columns, "rows": rows, "truncated": False}
        ),
        chart_category_column=category_col,
    )


print("=" * 70)
print("TEST 1: vague pie-ish question, 12+ real categories -> downgrade to bar")
print("=" * 70)

many_categories = [[f"Category {i}", i * 3] for i in range(1, 13)]  # 12 categories
state1 = _state(
    "pie chart", "reasoned", ["category", "value"], many_categories,
    category_col="category", question="What's the breakdown of sales by category?",
)
result1 = validate_chart_shape(state1)
print("override result:", result1)
assert result1.get("chart_type") == "bar", (
    f"expected downgrade to 'bar', got {result1.get('chart_type')!r}"
)
assert result1.get("chart_type_override_note"), "override note must be set"
print("PASS: 12-category pie downgraded to bar.\n")


print("=" * 70)
print("TEST 2: 'trend over time' question, non-temporal category -> line downgrades to bar")
print("=" * 70)

non_temporal_rows = [
    ["Electronics", 100], ["Apparel", 80], ["Home Goods", 60], ["Toys", 40],
]
state2 = _state(
    "line chart", "reasoned", ["category", "revenue"], non_temporal_rows,
    category_col="category", question="Show revenue trend over time by category.",
)
result2 = validate_chart_shape(state2)
print("override result:", result2)
assert result2.get("chart_type") == "bar", (
    f"expected downgrade to 'bar', got {result2.get('chart_type')!r}"
)
assert result2.get("chart_type_override_note"), "override note must be set"
assert "line" in result2["chart_type_override_note"].lower()
print("PASS: line chart with a non-temporal category column downgraded to bar, "
      f"note: {result2['chart_type_override_note']!r}\n")

# Sanity: a REAL temporal category column must NOT be downgraded.
temporal_rows = [
    ["2024-01-01", 100], ["2024-02-01", 120], ["2024-03-01", 90],
]
state2b = _state(
    "line chart", "reasoned", ["month", "revenue"], temporal_rows,
    category_col="month", question="Show revenue trend over time.",
)
result2b = validate_chart_shape(state2b)
assert result2b == {}, f"a genuinely temporal category must NOT be overridden, got {result2b}"
print("PASS: genuinely temporal category column is NOT downgraded (no false positive).\n")


print("=" * 70)
print("TEST 3: explicit 'pie chart' request, 15-category result -> bar with")
print("explicit ask/rendered/why disclosure")
print("=" * 70)

fifteen_categories = [[f"Segment {i}", i] for i in range(1, 16)]
state3 = _state(
    "pie chart", "explicit", ["segment", "share"], fifteen_categories,
    category_col="segment", question="Show me a pie chart of share by segment.",
)
result3 = validate_chart_shape(state3)
print("override result:", result3)
assert result3.get("chart_type") == "bar", (
    f"expected downgrade to 'bar', got {result3.get('chart_type')!r}"
)
note = result3.get("chart_type_override_note", "")
assert note, "override note must be set for an explicit-type override"
assert "pie" in note.lower(), f"note must name what was asked for: {note!r}"
assert "bar" in note.lower(), f"note must name what was rendered instead: {note!r}"
assert "because" in note.lower(), f"note must explain why: {note!r}"
print(f"PASS: explicit pie-chart request downgraded with disclosure: {note!r}\n")


print("=" * 70)
print("TEST 4: a genuinely compliant pie chart (<=5 categories) is left untouched")
print("=" * 70)

five_categories = [["A", 10], ["B", 20], ["C", 30], ["D", 15], ["E", 25]]
state4 = _state(
    "pie chart", "explicit", ["segment", "share"], five_categories,
    category_col="segment", question="Show me a pie chart of share by segment.",
)
result4 = validate_chart_shape(state4)
assert result4 == {}, f"a compliant 5-category pie chart must NOT be overridden, got {result4}"
print("PASS: compliant pie chart (5 categories) is untouched.\n")


print("=" * 70)
print("TEST 5: scatter with fewer than 2 real numeric measures -> bar")
print("=" * 70)

state5 = _state(
    "scatter plot", "reasoned", ["industry", "avg_rating"],
    [["Tech", 4.1], ["Finance", 3.8], ["Retail", 3.5]],
    category_col="industry", question="Relationship between industry and rating.",
)
result5 = validate_chart_shape(state5)
print("override result:", result5)
assert result5.get("chart_type") == "bar", result5
print("PASS: scatter with only 1 real numeric column downgraded to bar.\n")

# Sanity: 2 real numeric measures (excluding id-like columns) must pass through.
state5b = _state(
    "scatter plot", "reasoned", ["industry_id", "avg_rating", "avg_salary"],
    [["1", 4.1, 90000], ["2", 3.8, 85000], ["3", 3.5, 95000]],
    category_col="industry_id", question="Relationship between rating and salary.",
)
result5b = validate_chart_shape(state5b)
assert result5b == {}, f"2 real numeric measures (excluding id col) must pass, got {result5b}"
print("PASS: scatter with 2 real numeric measures (id column excluded) is untouched.\n")


print("=" * 70)
print("TEST 6: stacked bar with fewer than 3 columns -> plain bar")
print("=" * 70)

state6 = _state(
    "stacked bar chart", "explicit", ["category", "value"],
    [["A", 10], ["B", 20]],
    category_col="category", question="Stacked bar of category by value.",
)
result6 = validate_chart_shape(state6)
print("override result:", result6)
assert result6.get("chart_type") == "bar", result6
print("PASS: stacked bar with only 2 columns downgraded to plain bar.\n")


print("=" * 70)
print("TEST 7: histogram with too few rows -> bar")
print("=" * 70)

state7 = _state(
    "histogram", "explicit", ["salary"],
    [[50000], [60000], [70000]],
    category_col="salary", question="Show a histogram of salary.",
)
result7 = validate_chart_shape(state7)
print("override result:", result7)
assert result7.get("chart_type") == "bar", result7
print("PASS: histogram with only 3 rows downgraded to bar.\n")

print("=" * 70)
print("ALL VALIDATE_CHART_SHAPE ASSERTIONS PASSED")
print("=" * 70)
