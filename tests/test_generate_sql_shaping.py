"""Standalone tests for the three new/fixed SQL-shaping instructions in generate_sql.

All tests: real add_context call for live schema, then generate_sql directly.
No full-graph overhead; no LLM calls beyond generate_sql itself.

Test 1 — treemap: question implying a two-level hierarchy
  Confirms GROUP BY on both the outer category and the inner sub-category
  (not just one level), plus an aggregate.

Test 2 — binning: question implying bucketing a continuous value
  Confirms the generated SQL uses a CASE WHEN bucket rather than returning
  raw ungrouped values.

Test 3 — pivot (long-to-wide): question needing one column per category value
  Confirms conditional aggregation (CASE WHEN inside an aggregate).

Test 4 — unpivot (wide-to-long): question needing columns stacked into rows
  Confirms UNION ALL reshaping.
"""

from agents.sql_analyst import add_context, generate_sql
from models.schema import SQLAnalystState

print("=" * 70)
print("TEST 1: treemap — two-level hierarchy, GROUP BY both levels + aggregate")
print("=" * 70)
state1 = SQLAnalystState(
    wants_visualization=True,
    chart_type="treemap",
    curated_question=(
        "Show total revenue as a treemap, grouped by product category "
        "as the outer level and seller state as the inner level."
    ),
)
ctx1 = add_context(state1)
state1 = state1.model_copy(update=ctx1)
sql1 = generate_sql(state1)["generated_sql_query"]
print("--- generated SQL ---")
print(sql1)
print()

sql1_upper = sql1.upper()
# Must GROUP BY two columns (outer + inner hierarchy levels)
assert sql1_upper.count("GROUP BY") >= 1, "treemap SQL must have GROUP BY"
# Must have an aggregate for the size dimension
assert any(agg in sql1_upper for agg in ("SUM(", "COUNT(", "SUM (", "COUNT (")), (
    "treemap SQL must include an aggregate (SUM or COUNT) for the size dimension"
)
# Must reference two grouping levels — count comma-separated items in GROUP BY clause
# (rough check: at least two distinct column references after GROUP BY)
group_by_idx = sql1_upper.find("GROUP BY")
group_by_clause = sql1_upper[group_by_idx:]
# Either GROUP BY has a comma (two+ items) or there are two GROUP BY clauses
has_two_levels = "," in group_by_clause.split("\n")[0] or sql1_upper.count("GROUP BY") >= 2
assert has_two_levels, (
    "treemap SQL must GROUP BY both the outer and inner hierarchy levels "
    f"(found clause: {group_by_clause[:120]!r})"
)
print("PASS: treemap SQL groups by two hierarchy levels with an aggregate.\n")

print("=" * 70)
print("TEST 2: binning — CASE WHEN bucket, not raw ungrouped values")
print("=" * 70)
state2 = SQLAnalystState(
    wants_visualization=True,
    chart_type="bar chart",
    curated_question=(
        "Show the number of orders grouped by payment value range "
        "(under $50, $50-$99, $100-$199, $200 and above)."
    ),
)
ctx2 = add_context(state2)
state2 = state2.model_copy(update=ctx2)
sql2 = generate_sql(state2)["generated_sql_query"]
print("--- generated SQL ---")
print(sql2)
print()

sql2_upper = sql2.upper()
assert "CASE" in sql2_upper and "WHEN" in sql2_upper, (
    "binning SQL must use CASE WHEN to create buckets, not return raw values"
)
assert "GROUP BY" in sql2_upper, "binning SQL must GROUP BY the bucket expression"
assert any(agg in sql2_upper for agg in ("COUNT(", "COUNT (")), (
    "binning SQL must aggregate (COUNT) within each bucket"
)
print("PASS: binning SQL uses CASE WHEN buckets with GROUP BY and COUNT.\n")

print("=" * 70)
print("TEST 3: pivot (long-to-wide) — CASE WHEN inside aggregate per column")
print("=" * 70)
state3 = SQLAnalystState(
    wants_visualization=True,
    chart_type="bar chart",
    curated_question=(
        "Show monthly order counts as separate columns for the top two customer states "
        "SP and RJ, one row per month."
    ),
)
ctx3 = add_context(state3)
state3 = state3.model_copy(update=ctx3)
sql3 = generate_sql(state3)["generated_sql_query"]
print("--- generated SQL ---")
print(sql3)
print()

sql3_upper = sql3.upper()
# PostgreSQL supports two valid conditional-aggregation forms that both produce pivot columns:
# - CASE WHEN ... THEN ... ELSE 0 END inside SUM/COUNT (standard SQL)
# - COUNT(*) FILTER (WHERE ...) / SUM(x) FILTER (WHERE ...) (PostgreSQL-native, idiomatic)
# Accept either; both produce one aggregate column per category, which is the correct shape.
uses_case_when = "CASE" in sql3_upper and "WHEN" in sql3_upper
uses_filter = "FILTER" in sql3_upper
assert uses_case_when or uses_filter, (
    "pivot SQL must use either CASE WHEN inside an aggregate, or FILTER (WHERE ...), "
    "to produce one output column per pivoted category value"
)
assert "GROUP BY" in sql3_upper, "pivot SQL must GROUP BY the time/row dimension"
# At least two aggregate expressions (one per pivoted category column)
agg_count = (
    sql3_upper.count("SUM(") + sql3_upper.count("COUNT(")
    + sql3_upper.count("SUM (") + sql3_upper.count("COUNT (")
)
assert agg_count >= 2, (
    f"pivot SQL must produce at least two aggregate columns (one per pivoted category), found {agg_count}"
)
technique = "CASE WHEN" if uses_case_when else "FILTER (WHERE ...)"
print(f"PASS: pivot SQL uses {technique} conditional aggregation and GROUP BY.\n")

print("=" * 70)
print("TEST 4: unpivot (wide-to-long) — UNION ALL stacking columns into rows")
print("=" * 70)
state4 = SQLAnalystState(
    wants_visualization=True,
    chart_type="bar chart",
    curated_question=(
        "I have monthly revenue and monthly order count as separate metrics — "
        "stack them into rows so I get one row per metric per month, "
        "with a metric_name column and a value column."
    ),
)
ctx4 = add_context(state4)
state4 = state4.model_copy(update=ctx4)
sql4 = generate_sql(state4)["generated_sql_query"]
print("--- generated SQL ---")
print(sql4)
print()

sql4_upper = sql4.upper()
assert "UNION" in sql4_upper, (
    "unpivot SQL must use UNION ALL to stack metric columns into rows"
)
print("PASS: unpivot SQL uses UNION to stack columns into rows.\n")

print("=" * 70)
print("ALL SQL SHAPING TESTS PASSED")
print("=" * 70)
