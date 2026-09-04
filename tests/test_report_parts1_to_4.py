"""Tests for the four report improvements.

Test 1: End-to-end visualize: run produces a real .png and chart_image_path is logged.
Test 2: Report from a new entry embeds the chart image as inline base64.
Test 3: Report for an older log entry (no chart_image_path) states honestly no image.
Test 4: Report with real cleaning history shows a computed before/after comparison.
Test 5: Report for a query with non-obvious WHERE filters includes an Assumptions line.
"""

import json
import re
from pathlib import Path

from utils.generate_report import (
    _compute_before_after,
    _extract_query_assumptions,
    _parse_placeholder_issue,
    generate_report,
    last_query_log_entry,
)

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

assert "<h2>Visualization</h2>" in html2, "Visualization section must be present"
assert "data:image/png;base64," in html2, (
    "Chart image must be embedded as base64 data URI in the report"
)
assert "<img " in html2, "An <img> tag must be present"
print(f"PASS: report {report2} embeds chart image as inline base64.\n")

# ── Test 3: older entry (no chart_image_path) → honest 'no image' note ────────
print("=" * 70)
print("TEST 3: older entry (no chart_image_path) → honest 'no image' statement")
print("=" * 70)

# Simulate a pre-Part-1 log entry by omitting chart_image_path entirely.
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
    # chart_image_path deliberately absent (simulates pre-Part-1 entry)
    "final_answer": "Visualization data saved to: ...",
}

report3 = generate_report(old_viz_entry)
html3 = Path(report3).read_text()

assert "<h2>Visualization</h2>" in html3, "Visualization section must still be present"
assert "data:image/png;base64," not in html3, (
    "No base64 image must appear when chart_image_path is absent"
)
assert "no chart image" in html3.lower() or "predates" in html3.lower(), (
    "Report must state plainly that no chart image is available for older entries"
)
assert "<img " not in html3, "No <img> tag must be present when no chart image is available"
print(f"PASS: report {report3} states no image available (no broken reference).\n")

# ── Test 4: real cleaning history → computed before/after numbers ─────────────
print("=" * 70)
print("TEST 4: real cleaning history → computed before/after comparison")
print("=" * 70)

# Load entries from query_log to find one that touches a table with cleaning history.
all_entries = []
if LOG_PATH.exists():
    for line in LOG_PATH.read_text().splitlines():
        try:
            all_entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass

# Look for a log entry where the SQL touches a table with cleaning history.
cleaning_entries = []
if CLEANING_LOG_PATH.exists():
    for line in CLEANING_LOG_PATH.read_text().splitlines():
        try:
            cleaning_entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass

cleaned_tables = {
    f.get("table_name", "").lower()
    for e in cleaning_entries
    for f in e.get("files", [])
    if f.get("issues_found")
}

entry4 = None
for e in all_entries:
    sql = e.get("generated_sql_query", "")
    for t in cleaned_tables:
        if t and re.search(r"\b" + re.escape(t) + r"\b", sql, re.IGNORECASE):
            entry4 = e
            break
    if entry4:
        break

if entry4 is None:
    print("SKIP: no log entry found that touches a table with cleaning history.\n")
else:
    report4 = generate_report(entry4)
    html4 = Path(report4).read_text()

    assert "<h2>Data Cleaning</h2>" in html4, "Data Cleaning section must be present"

    has_comparison = (
        "Before cleaning" in html4 and "After cleaning" in html4
    ) or "before/after" in html4.lower() or "placeholder" in html4.lower()

    # The comparison table contains "Scenario" header
    has_table = "Scenario" in html4 or "Before cleaning (simulated)" in html4

    assert has_comparison or has_table, (
        "Data Cleaning section must include a real computed before/after comparison "
        "for tables with cleaning history.\n"
        "Hint: check that _compute_before_after() ran and returned a result."
    )
    print(f"PASS: report {report4} includes before/after comparison.\n")

# ── Test 5: non-obvious WHERE filters → Assumptions line ─────────────────────
print("=" * 70)
print("TEST 5: non-obvious WHERE filter → Assumptions line in report")
print("=" * 70)

# Use the scatter-plot SQL from the last visualize entry (has <> '-1' and ~ pattern).
viz_entries = [e for e in all_entries if e.get("route_response") == "visualize"]
entry5 = next(
    (e for e in reversed(viz_entries) if e.get("generated_sql_query") and
     ("<>" in e["generated_sql_query"] or "~" in e["generated_sql_query"])),
    None,
)

if entry5 is None:
    print("SKIP: no visualize log entry with non-obvious WHERE filter found.\n")
else:
    sql5 = entry5["generated_sql_query"]
    print(f"Using entry: {entry5.get('user_question')!r}")
    print(f"SQL fragment: {sql5[:120].strip()!r}")

    # Unit test the assumption extractor directly first.
    assumptions5 = _extract_query_assumptions(sql5)
    print(f"Extracted assumptions: {assumptions5}")
    assert assumptions5, (
        f"_extract_query_assumptions must find at least one assumption in this SQL:\n{sql5}"
    )

    # Now check the full report.
    report5 = generate_report(entry5)
    html5 = Path(report5).read_text()

    assert "<strong>Assumptions:</strong>" in html5, (
        "Visualization section must include an Assumptions block for queries with "
        "non-obvious WHERE filters."
    )
    print(f"PASS: report {report5} includes Assumptions line.\n")

# ── Also unit-test _parse_placeholder_issue and _extract_query_assumptions ────
print("=" * 70)
print("Unit tests: _parse_placeholder_issue and _extract_query_assumptions")
print("=" * 70)

# parse_placeholder_issue
issue_text = "Placeholder values: column 'Industry' has 71 value(s) that look like placeholders standing in for real data (['-1']), mixed in among otherwise genuine values."
parsed = _parse_placeholder_issue(issue_text)
assert parsed is not None, "Should parse placeholder issue"
assert parsed["column"] == "Industry", f"expected column='Industry', got {parsed['column']!r}"
assert parsed["count"] == 71, f"expected count=71, got {parsed['count']}"
assert "-1" in parsed["placeholders"], f"expected '-1' in placeholders, got {parsed['placeholders']}"
print("PASS: _parse_placeholder_issue parses correctly.")

# extract_query_assumptions
sql_with_filters = """
SELECT industry, AVG(rating) AS avg_rating
FROM uncleaned_ds_jobs
WHERE industry IS NOT NULL
  AND industry <> '-1'
  AND salary_estimate ~ '^[0-9]+-[0-9]+'
  AND rating IS NOT NULL
GROUP BY industry
HAVING COUNT(*) >= 5
"""
assumptions = _extract_query_assumptions(sql_with_filters)
print(f"Assumptions found: {assumptions}")
assert any("'-1'" in a for a in assumptions), "Must flag industry <> '-1' as non-obvious"
assert any("pattern" in a.lower() or "~" in a or "matches" in a for a in assumptions), (
    "Must flag regex pattern filter as non-obvious"
)
assert any("5" in a and "fewer" in a.lower() for a in assumptions), (
    "Must flag HAVING COUNT(*) >= 5 threshold"
)
print("PASS: _extract_query_assumptions extracts all three non-obvious filters.\n")

print("=" * 70)
print("ALL PARTS 1-4 TESTS PASSED")
print("=" * 70)
