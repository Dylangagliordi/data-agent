"""
Regression test confirming that truncated results (execute_sql capped at
MAX_RESULT_ROWS) are handled correctly end-to-end through build_visualization.

The new structured JSON format stores truncation as a boolean field
{"columns":..., "rows":..., "truncated": true} rather than a prefixed string,
so _parse_sql_result simply reads the flag and returns (rows, True).

This test proves:
1. _parse_sql_result correctly reads was_truncated=True and real row data from
   a truncated JSON payload.
2. build_visualization writes a CSV containing the real (partial) rows, not
   an empty "(no data returned)" placeholder.
3. final_answer explicitly discloses the truncation rather than claiming the
   result was empty.
"""

import csv
import json
from pathlib import Path

from agents.sql_analyst import MAX_RESULT_ROWS, _parse_sql_result, build_visualization
from models.schema import SQLAnalystState

# Mirrors execute_sql's new structured JSON format with truncated=True.
TRUNCATED_RESULT = json.dumps({
    "columns": ["industry", "rating", "avg_salary_estimate"],
    "rows": [
        ["Insurance Carriers", 3.1, 154000.0],
        ["Research & Development", 4.2, 154000.0],
        ["Consulting", 3.8, 154000.0],
    ],
    "truncated": True,
})

print("=" * 70)
print("STEP 1: _parse_sql_result parses real rows out of a truncated result")
print("=" * 70)

rows, was_truncated = _parse_sql_result(TRUNCATED_RESULT)
print("parsed rows:", rows)
print("was_truncated:", was_truncated)

assert was_truncated is True, "expected was_truncated=True for a truncated result"
assert rows == [
    {"industry": "Insurance Carriers", "rating": 3.1, "avg_salary_estimate": 154000.0},
    {"industry": "Research & Development", "rating": 4.2, "avg_salary_estimate": 154000.0},
    {"industry": "Consulting", "rating": 3.8, "avg_salary_estimate": 154000.0},
], f"expected the 3 real rows to parse out, got {rows}"
print("PASSED: real rows recovered from truncated JSON payload.\n")

print("=" * 70)
print("STEP 2: build_visualization writes a real CSV, not an empty placeholder")
print("=" * 70)

state = SQLAnalystState(
    wants_visualization=True,
    user_question="Which industries have the highest concentration of top-rated employers?",
    curated_question="Which industries have the highest concentration of top-rated employers?",
    chart_type="scatter plot",
    chart_type_source="reasoned",
    chart_type_reasoning="Relationship between two numeric variables across industries.",
    sql_query_execution_result=TRUNCATED_RESULT,
)
result = build_visualization(state)
print("output_file_path:", result["output_file_path"])
print("final_answer:\n", result["final_answer"])

csv_path = Path(result["output_file_path"])
assert csv_path.exists(), f"CSV file must exist at {csv_path}"
with open(csv_path) as f:
    reader = csv.DictReader(f)
    csv_rows = list(reader)

assert len(csv_rows) == 3, f"expected 3 real data rows in the CSV, got {csv_rows}"
assert csv_rows[0]["Industry"] == "Insurance Carriers", f"got {csv_rows[0]}"
print("PASSED: CSV contains the real partial rows, not '(no data returned)'.\n")

print("=" * 70)
print("STEP 3: final_answer discloses the truncation, doesn't claim empty result")
print("=" * 70)

answer_lower = result["final_answer"].lower()
assert "capped" in answer_lower or "matched more rows" in answer_lower, (
    f"expected an explicit truncation disclosure in final_answer, got: {result['final_answer']}"
)
assert "no results" not in answer_lower and "no data" not in answer_lower, (
    f"final_answer must not claim the query returned no data when it actually matched rows: "
    f"{result['final_answer']}"
)
print(f"PASSED: final_answer honestly discloses truncation (cap={MAX_RESULT_ROWS}), "
      "never claims an empty result.\n")

print("=" * 70)
print("ALL VISUALIZATION-TRUNCATION ASSERTIONS PASSED")
print("=" * 70)
