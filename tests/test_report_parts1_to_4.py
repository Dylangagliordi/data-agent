"""Chart-image rendering + report chart-image embedding.

Test 1: End-to-end visualize: run produces a real .png and chart_image_path is
        logged; every chart type renders without crashing (unchanged, still
        exercises agents.sql_analyst.build_visualization directly).
Test 2: Report from a new entry (real chart_image_path) embeds the chart
        image as inline base64 in its "Visualize the result as a ..." step
        (Spec 2 — the report body is now the shared narrative walkthrough,
        not a dedicated "Visualization" section).
Test 3: Report for an entry with no chart_image_path states honestly that no
        image is available, in that same narrative step.

Tests 4/5 of the original Parts 1-4 spec (a live-DB before/after placeholder
comparison, and a general WHERE-filter "Assumptions" line) tested
utils/generate_report.py internals that Spec 2 (Final) deliberately removed
when it rewired both documents to render from one shared narrative
walkthrough (utils/narrative.py) — see that spec for why. Assumption-style
disclosure now lives in the final_answer text itself
(agents/sql_analyst.py:_analyst_judgment_disclosure), which the walkthrough's
"The final result" step quotes verbatim — see tests/test_narrative_walkthrough.py.
"""

import json
from pathlib import Path

from utils.generate_report import generate_report

LOG_PATH = Path("logs/query_log.jsonl")
CLEANING_LOG_PATH = Path("logs/cleaning_log.jsonl")

# ── Test 1: real visualize: run → .png file + chart_image_path in log ─────────
print("=" * 70)
print("TEST 1: visualize: run → real .png + chart_image_path logged")
print("=" * 70)

from agents.sql_analyst import build_visualization
from models.schema import SQLAnalystState

import json as _json_fmt
FAKE_RESULT = _json_fmt.dumps({
    "columns": ["customer_state", "order_count"],
    "rows": [["SP", 41746], ["RJ", 12852], ["MG", 11635]],
    "truncated": False,
})

state = SQLAnalystState(
    wants_visualization=True,
    user_question="Show a bar chart of orders per state",
    curated_question="Show a bar chart of orders per state.",
    chart_type="bar chart",
    chart_type_source="explicit",
    chart_type_reasoning="",
    sql_query_execution_result=FAKE_RESULT,
)
result = build_visualization(state)

assert "chart_image_path" in result, "build_visualization must return chart_image_path"
assert result["chart_image_path"], "chart_image_path must be a non-empty string"
png_path = Path(result["chart_image_path"])
assert png_path.exists(), f"PNG file must exist at {png_path}"
assert png_path.suffix == ".png", f"chart_image_path must point to a .png, got {png_path}"
assert png_path.stat().st_size > 0, "PNG file must be non-empty"

# The PNG slug must match the CSV slug (same base filename, different extension).
csv_path = Path(result["output_file_path"])
assert png_path.stem == csv_path.stem, (
    f"PNG stem {png_path.stem!r} must match CSV stem {csv_path.stem!r}"
)
print(f"PASS: PNG generated at {png_path} ({png_path.stat().st_size:,} bytes)\n")

# Test various chart types render without crashing.
for ct in ["line chart", "scatter plot", "pie chart", "donut chart",
           "histogram", "box plot", "stacked bar chart", "treemap"]:
    s = SQLAnalystState(
        wants_visualization=True,
        user_question=f"test {ct}",
        curated_question=f"Test {ct}.",
        chart_type=ct,
        chart_type_source="explicit",
        chart_type_reasoning="",
        sql_query_execution_result=_json_fmt.dumps({
            "columns": ["category", "value"],
            "rows": [["A", 10], ["B", 20], ["C", 30]],
            "truncated": False,
        }),
    )
    r = build_visualization(s)
    # chart_image_path may be empty for chart types where data shape doesn't fit
    # (e.g. scatter needs 2 numeric cols) — but must never crash the build.
    assert "output_file_path" in r, f"build_visualization must always return output_file_path for {ct}"
    print(f"  {ct}: chart_image_path={r.get('chart_image_path', '(empty)')!r:.60}")

print("PASS: all chart types render without crashing.\n")

# ── Test 2: report from new entry embeds chart image inline ───────────────────
print("=" * 70)
print("TEST 2: report embeds chart image as inline base64")
print("=" * 70)

# Use the entry we just created (chart_image_path is set).
synthetic_viz_entry = {
    "timestamp": "2026-09-02T00:00:00+00:00",
    "route_response": "visualize",
    "route_comments": "",
    "user_question": "Show a bar chart of orders per state",
    "curated_question": "Show a bar chart of orders per state.",
    "chart_type": "bar chart",
    "chart_type_source": "explicit",
    "chart_type_reasoning": "",
    "generated_sql_query": "SELECT customer_state, COUNT(*) AS order_count FROM orders GROUP BY customer_state",
    "is_safe": "yes",
    "sql_query_execution_result": FAKE_RESULT,
    "output_file_path": result["output_file_path"],
    "chart_image_path": result["chart_image_path"],  # present and valid
    "final_answer": result["final_answer"],
}

report2 = generate_report(synthetic_viz_entry)
html2 = Path(report2).read_text()

assert "Visualize the result as a bar chart" in html2, (
    "the walkthrough's chart step must be present"
)
assert "data:image/png;base64," in html2, (
    "Chart image must be embedded as base64 data URI in the report"
)
assert "<img " in html2, "An <img> tag must be present"
print(f"PASS: report {report2} embeds chart image as inline base64.\n")

# ── Test 3: entry with no chart_image_path → honest 'no image' note ───────────
print("=" * 70)
print("TEST 3: entry with no chart_image_path → honest 'no image' statement")
print("=" * 70)

old_viz_entry = {
    "timestamp": "2026-01-01T00:00:00+00:00",
    "route_response": "visualize",
    "route_comments": "",
    "user_question": "Show the number of orders per state",
    "curated_question": "Show the number of orders per state.",
    "chart_type": "bar chart",
    "chart_type_source": "explicit",
    "chart_type_reasoning": "",
    "generated_sql_query": "SELECT customer_state, COUNT(*) FROM orders GROUP BY customer_state",
    "is_safe": "yes",
    "sql_query_execution_result": FAKE_RESULT,
    "output_file_path": result["output_file_path"],
    # chart_image_path deliberately absent.
    "final_answer": "Visualization data saved to: ...",
}

report3 = generate_report(old_viz_entry)
html3 = Path(report3).read_text()

assert "Visualize the result as a bar chart" in html3, "the walkthrough's chart step must still be present"
assert "data:image/png;base64," not in html3, (
    "No base64 image must appear when chart_image_path is absent"
)
assert "no chart image is available" in html3.lower(), (
    "Report must state plainly that no chart image is available"
)
assert "<img " not in html3, "No <img> tag must be present when no chart image is available"
print(f"PASS: report {report3} states no image available (no broken reference).\n")

print("=" * 70)
print("ALL CHART-IMAGE-RENDERING TESTS PASSED")
print("=" * 70)
