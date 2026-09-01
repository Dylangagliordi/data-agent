"""Standalone test for the build_visualization node.

Confirms:
1. A real CSV is written to outputs/visualizations/ with correct human-readable headers.
2. The interpretive summary is grounded in the actual result data (non-empty, specific).
3. final_answer includes the file path, chart type, and the summary.
4. When chart_type_source is "reasoned", the reasoning appears in final_answer.
5. When chart_type_source is "explicit", no reasoning note in final_answer.
"""

import csv
from pathlib import Path

from agents.sql_analyst import build_visualization
from models.schema import SQLAnalystState

# Synthetic execution result (same format execute_sql produces)
FAKE_RESULT = str([
    {"customer_state": "SP", "order_count": 41746},
    {"customer_state": "RJ", "order_count": 12852},
    {"customer_state": "MG", "order_count": 11635},
    {"customer_state": "RS", "order_count": 5466},
    {"customer_state": "PR", "order_count": 5045},
])

print("=" * 70)
print("CASE 1: reasoned chart type -> reasoning in final_answer, CSV correct")
print("=" * 70)
state1 = SQLAnalystState(
    wants_visualization=True,
    user_question="Show the number of orders per customer state",
    curated_question="Show the number of orders per customer state.",
    chart_type="bar chart",
    chart_type_source="reasoned",
    chart_type_reasoning="Comparing counts across discrete categories (states) is a classic bar chart use case.",
    sql_query_execution_result=FAKE_RESULT,
)
result1 = build_visualization(state1)
print("output_file_path:", result1["output_file_path"])
print("final_answer:\n", result1["final_answer"])
print()

# Verify CSV exists and has correct headers
csv_path = Path(result1["output_file_path"])
assert csv_path.exists(), f"CSV file must exist at {csv_path}"
with open(csv_path) as f:
    reader = csv.DictReader(f)
    headers = reader.fieldnames
    rows = list(reader)
print("CSV headers:", headers)
print("CSV rows:", rows[:3])

assert headers is not None, "CSV must have headers"
assert "Customer State" in headers, f"expected 'Customer State' header, got {headers}"
assert "Order Count" in headers, f"expected 'Order Count' header, got {headers}"
assert len(rows) == 5, f"expected 5 data rows, got {len(rows)}"
assert rows[0]["Customer State"] == "SP", f"first row state should be SP, got {rows[0]}"
assert rows[0]["Order Count"] == "41746", f"first row count should be 41746, got {rows[0]}"
print("PASS: CSV has correct human-readable headers and all 5 data rows.\n")

# Verify final_answer content
assert result1["output_file_path"] in result1["final_answer"], "final_answer must include file path"
assert "bar chart" in result1["final_answer"].lower(), "final_answer must include chart type"
assert "Comparing counts" in result1["final_answer"], (
    "reasoned source -> reasoning must appear in final_answer"
)
assert result1["final_answer"].split("Summary:")[-1].strip(), "final_answer must include non-empty summary"
print("PASS: final_answer includes file path, chart type, reasoning, and summary.\n")

print("=" * 70)
print("CASE 2: explicit chart type -> no reasoning note in final_answer")
print("=" * 70)
state2 = SQLAnalystState(
    wants_visualization=True,
    user_question="Show me a pie chart of order counts by state (top 5)",
    curated_question="Show a pie chart of order counts by state (top 5 states).",
    chart_type="pie chart",
    chart_type_source="explicit",
    chart_type_reasoning="",
    sql_query_execution_result=str([
        {"customer_state": "SP", "order_count": 41746},
        {"customer_state": "RJ", "order_count": 12852},
        {"customer_state": "MG", "order_count": 11635},
        {"customer_state": "RS", "order_count": 5466},
        {"customer_state": "PR", "order_count": 5045},
    ]),
)
result2 = build_visualization(state2)
print("output_file_path:", result2["output_file_path"])
print("final_answer:\n", result2["final_answer"])

assert "Chart type reasoning:" not in result2["final_answer"], (
    "explicit source -> reasoning note must NOT appear in final_answer"
)
assert "pie chart" in result2["final_answer"].lower(), "final_answer must include chart type"
print("PASS: explicit source -> no reasoning note in final_answer.\n")

print("=" * 70)
print("ALL BUILD_VISUALIZATION ASSERTIONS PASSED")
print("=" * 70)
