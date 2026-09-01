"""Standalone test for generate_sql with wants_visualization=True.

Confirms the produced SQL is shaped correctly for the chart type:
- line chart request -> real GROUP BY time period + ORDER BY chronologically
- bar chart request -> real GROUP BY category + ORDER BY aggregate

Reuses add_context + determine_chart_type to get real state before calling
generate_sql, so the test exercises the actual prompting chain.
"""

from agents.sql_analyst import add_context, determine_chart_type, generate_sql
from models.schema import SQLAnalystState

print("=" * 70)
print("CASE 1: line chart -> expects GROUP BY time period + ORDER BY it")
print("=" * 70)
state = SQLAnalystState(
    wants_visualization=True,
    curated_question="Show me a line chart of the number of orders per month.",
)
ctx = add_context(state)
state = state.model_copy(update=ctx)

chart = determine_chart_type(state)
state = state.model_copy(update=chart)
print(f"chart_type: {state.chart_type}, source: {state.chart_type_source}")

sql_result = generate_sql(state)
sql = sql_result["generated_sql_query"]
print("--- generated SQL ---")
print(sql)
print()

sql_upper = sql.upper()
assert "GROUP BY" in sql_upper, "line chart SQL must contain GROUP BY for time period"
assert "ORDER BY" in sql_upper, "line chart SQL must contain ORDER BY for chronological ordering"
print("PASS: line chart SQL contains GROUP BY and ORDER BY.\n")

print("=" * 70)
print("CASE 2: bar chart (category comparison) -> GROUP BY + ORDER BY aggregate")
print("=" * 70)
state2 = SQLAnalystState(
    wants_visualization=True,
    curated_question="Show me a bar chart of total order count by customer state.",
)
ctx2 = add_context(state2)
state2 = state2.model_copy(update=ctx2)

chart2 = determine_chart_type(state2)
state2 = state2.model_copy(update=chart2)
print(f"chart_type: {state2.chart_type}, source: {state2.chart_type_source}")

sql_result2 = generate_sql(state2)
sql2 = sql_result2["generated_sql_query"]
print("--- generated SQL ---")
print(sql2)
print()

sql2_upper = sql2.upper()
assert "GROUP BY" in sql2_upper, "bar chart SQL must contain GROUP BY for category"
assert "ORDER BY" in sql2_upper, "bar chart SQL must contain ORDER BY"
print("PASS: bar chart SQL contains GROUP BY and ORDER BY.\n")

print("=" * 70)
print("ALL GENERATE_SQL VISUALIZATION ASSERTIONS PASSED")
print("=" * 70)
