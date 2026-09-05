"""
Regression test for the chart metric mis-selection bug: chart renderers used
to pick which SQL result column to plot BY POSITION (cols[0]/cols[1]), not by
meaning. A real query returning `industry, avg_salary, avg_rating, job_count`
for a question about employee SATISFACTION plotted avg_salary (position 1)
instead of avg_rating, purely because salary happened to be selected first in
the SQL — the chart silently answered the wrong question while the
separately-generated report prose correctly discussed rating.

The fixture data below is the REAL logged execution result from this
project's own query_log.jsonl for exactly that bug ("Of the 5 highest-paying
industries for data scientists, which offer the highest employee
satisfaction?"), reproduced here rather than invented.

Test 1 (live LLM): resolve_chart_columns on the real bug data picks
avg_rating (what the question actually asks about) as chart_value_column,
NOT avg_salary (which sits at SELECT-list position 1).

Test 2 (unit, mocked LLM failure): when the LLM pick fails/errors,
resolve_chart_columns falls back deterministically (first non-numeric column
as category, first numeric column that isn't the category as value) and sets
chart_column_resolution_note.

Test 3 (unit, no LLM): resolve_chart_columns is a no-op (returns {}) when the
result is empty or was truncated — nothing to resolve against.

Test 4 (unit, no LLM): the chart renderer itself (_chart_bar) plots the
EXPLICITLY resolved value_col, not the positional cols[1] — directly proving
the rendered bar heights are the rating values, not the salary values, for
the real bug data.

Test 5 (mocked LLM, deterministic): build_visualization appends
chart_column_resolution_note to final_answer when resolve_chart_columns had
to fall back — the note is never swallowed silently.
"""

import json

import agents.sql_analyst as sql_analyst_module
from agents.sql_analyst import (
    _chart_bar,
    _fallback_chart_columns,
    build_visualization,
    resolve_chart_columns,
)
from models.schema import SQLAnalystState

# Real logged data reproducing the salary-vs-rating bug (see module docstring).
BUG_QUESTION = (
    "Of the 5 highest-paying industries for data scientists, which offer the "
    "highest employee satisfaction?"
)
BUG_SQL = (
    "SELECT industry, avg_salary, avg_rating, job_count FROM industry_stats "
    "ORDER BY avg_rating DESC, avg_salary DESC;"
)
BUG_COLUMNS = ["industry", "avg_salary", "avg_rating", "job_count"]
BUG_ROWS = [
    ["Staffing & Outsourcing", 128971.42857142857, 4.137142857142858, 35],
    ["Federal Agencies", 134590.9090909091, 3.963636363636364, 11],
    ["Consulting", 131000.0, 3.8931034482758617, 31],
    ["Internet", 125625.0, 3.7849999999999993, 20],
    ["Aerospace & Defense", 139516.12903225806, 3.5580645161290327, 31],
]
BUG_RESULT_JSON = json.dumps(
    {"columns": BUG_COLUMNS, "rows": BUG_ROWS, "truncated": False}
)

print("=" * 70)
print("TEST 1 (live LLM): resolve_chart_columns picks avg_rating, not")
print("avg_salary, for a question about employee satisfaction")
print("=" * 70)

bug_state = SQLAnalystState(
    wants_visualization=True,
    curated_question=BUG_QUESTION,
    generated_sql_query=BUG_SQL,
    chart_type="bar chart",
    sql_query_execution_result=BUG_RESULT_JSON,
)
result1 = resolve_chart_columns(bug_state)
print("chart_category_column:", result1.get("chart_category_column"))
print("chart_value_column:", result1.get("chart_value_column"))
print("chart_secondary_column:", repr(result1.get("chart_secondary_column")))
print("chart_column_resolution_note:", repr(result1.get("chart_column_resolution_note")))

assert result1.get("chart_category_column") == "industry", (
    f"expected category_column='industry', got {result1.get('chart_category_column')!r}"
)
assert result1.get("chart_value_column") == "avg_rating", (
    f"BUG REPRODUCTION FAILED: expected value_column='avg_rating' (what the question "
    f"actually asks about), got {result1.get('chart_value_column')!r} — this is exactly "
    f"the positional mis-selection bug this fix addresses"
)
assert result1.get("chart_secondary_column") == "", (
    "secondary_column must be empty for a plain bar chart"
)
print("PASS: resolve_chart_columns correctly identifies avg_rating as the value "
      "column despite avg_salary appearing first in the SQL SELECT list.\n")


print("=" * 70)
print("TEST 2 (mocked LLM failure): deterministic fallback + resolution note")
print("=" * 70)


class _RaisingStructuredLLM:
    def invoke(self, messages):
        raise RuntimeError("simulated LLM failure")


class _RaisingLLM:
    def with_structured_output(self, schema_cls):
        return _RaisingStructuredLLM()


original_pick_llm = sql_analyst_module.pick_llm
sql_analyst_module.pick_llm = lambda level: _RaisingLLM()
try:
    result2 = resolve_chart_columns(bug_state)
    print("chart_category_column:", result2.get("chart_category_column"))
    print("chart_value_column:", result2.get("chart_value_column"))
    print("chart_column_resolution_note:", result2.get("chart_column_resolution_note"))

    # Deterministic fallback per spec: category -> first non-numeric column
    # ("industry"); value -> first numeric column that isn't the category
    # ("avg_salary", since it's the first numeric column in this fixture).
    assert result2.get("chart_category_column") == "industry", result2
    assert result2.get("chart_value_column") == "avg_salary", result2
    assert result2.get("chart_column_resolution_note"), (
        "chart_column_resolution_note must be non-empty when the LLM pick fails"
    )
    assert not result2.get("chart_secondary_column"), (
        "fallback path must never set a secondary column"
    )
    print("PASS: LLM failure triggers the deterministic fallback and sets "
          "chart_column_resolution_note — never raises.\n")

    # Direct unit check of the fallback helper itself.
    classification = {
        "industry": "non-numeric", "avg_salary": "numeric",
        "avg_rating": "numeric", "job_count": "numeric",
    }
    cat, val = _fallback_chart_columns(BUG_COLUMNS, classification)
    assert (cat, val) == ("industry", "avg_salary"), (cat, val)
    print("PASS: _fallback_chart_columns direct unit check matches spec.\n")
finally:
    sql_analyst_module.pick_llm = original_pick_llm


print("=" * 70)
print("TEST 3 (no LLM): resolve_chart_columns skips resolution for empty/")
print("truncated results, leaving chart_* fields unset")
print("=" * 70)

empty_state = SQLAnalystState(
    wants_visualization=True,
    curated_question=BUG_QUESTION,
    generated_sql_query=BUG_SQL,
    chart_type="bar chart",
    sql_query_execution_result=json.dumps({"columns": [], "rows": [], "truncated": False}),
)
result3a = resolve_chart_columns(empty_state)
assert result3a == {}, f"expected {{}} for empty result, got {result3a}"

truncated_state = SQLAnalystState(
    wants_visualization=True,
    curated_question=BUG_QUESTION,
    generated_sql_query=BUG_SQL,
    chart_type="bar chart",
    sql_query_execution_result=json.dumps(
        {"columns": BUG_COLUMNS, "rows": BUG_ROWS, "truncated": True}
    ),
)
result3b = resolve_chart_columns(truncated_state)
assert result3b == {}, f"expected {{}} for truncated result, got {result3b}"
print("PASS: empty and truncated results both skip resolution entirely (return {}).\n")


print("=" * 70)
print("TEST 4 (no LLM): _chart_bar plots the EXPLICIT value_col, not the")
print("positional cols[1] — proving the rendered chart uses the right metric")
print("=" * 70)

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

bug_data = [dict(zip(BUG_COLUMNS, row)) for row in BUG_ROWS]

# Old (buggy) positional behavior: cols[1] = avg_salary.
fig_old = Figure(figsize=(6, 4))
FigureCanvasAgg(fig_old)
ax_old = fig_old.add_subplot(111)
_chart_bar(ax_old, bug_data, BUG_COLUMNS)  # no explicit cols -> falls back to cols[1]
old_heights = [p.get_height() for p in ax_old.patches]

# Fixed behavior: explicit value_col="avg_rating" (what resolve_chart_columns picks).
fig_new = Figure(figsize=(6, 4))
FigureCanvasAgg(fig_new)
ax_new = fig_new.add_subplot(111)
_chart_bar(ax_new, bug_data, BUG_COLUMNS, category_col="industry", value_col="avg_rating")
new_heights = [p.get_height() for p in ax_new.patches]

expected_rating_heights = [row[2] for row in BUG_ROWS]
expected_salary_heights = [row[1] for row in BUG_ROWS]

assert old_heights == expected_salary_heights, (
    f"sanity check: unresolved call should still fall back to positional cols[1] "
    f"(avg_salary), got {old_heights}"
)
assert new_heights == expected_rating_heights, (
    f"explicit value_col='avg_rating' must produce bars matching the REAL rating "
    f"values {expected_rating_heights}, got {new_heights}"
)
assert new_heights != old_heights, (
    "resolved chart must plot different (correct) values than the old positional pick"
)
print(f"old (positional) bar heights: {old_heights}")
print(f"new (resolved)   bar heights: {new_heights}")
print("PASS: explicit value_col overrides positional selection; rendered bars now "
      "match the satisfaction metric the question actually asked about.\n")


print("=" * 70)
print("TEST 5 (mocked LLM): build_visualization appends")
print("chart_column_resolution_note to final_answer, never swallows it")
print("=" * 70)


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeSummaryLLM:
    def invoke(self, messages):
        return _FakeResponse(
            "Staffing & Outsourcing has the highest satisfaction among the top "
            "5 highest-paying industries."
        )


sql_analyst_module.pick_llm = lambda level: _FakeSummaryLLM()
try:
    viz_state = SQLAnalystState(
        wants_visualization=True,
        user_question=BUG_QUESTION,
        curated_question=BUG_QUESTION,
        generated_sql_query=BUG_SQL,
        chart_type="bar chart",
        chart_type_source="reasoned",
        chart_type_reasoning="Comparing satisfaction across industries.",
        sql_query_execution_result=BUG_RESULT_JSON,
        chart_category_column="industry",
        chart_value_column="avg_rating",
        chart_secondary_column="",
        chart_column_resolution_note=(
            "Could not confidently identify which column the question meant to "
            "chart — used the first available metric column instead."
        ),
    )
    result5 = build_visualization(viz_state)
    print("final_answer:\n", result5["final_answer"])
    assert (
        "Could not confidently identify which column the question meant to chart"
        in result5["final_answer"]
    ), "chart_column_resolution_note must be appended to final_answer, not swallowed"
    print("PASS: chart_column_resolution_note is surfaced in final_answer.\n")
finally:
    sql_analyst_module.pick_llm = original_pick_llm


print("=" * 70)
print("ALL CHART COLUMN RESOLUTION ASSERTIONS PASSED")
print("=" * 70)
