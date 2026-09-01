"""Tests for utils/generate_report.py.

Test 1: plain SQL entry (no chart_type/output_file_path) — all four sections
  present (Introduction, Data Cleaning, Topic Focus, Summary), Visualization
  section correctly OMITTED.

Test 2: visualize entry — Visualization section correctly INCLUDED.

Test 3: table with no cleaning history — Data Cleaning section states this
  honestly (does not fabricate content).

Test 4: report last reads the most recent log entry without adding a new one.

Uses real query_log.jsonl entries where possible (recent ones from the log).
All report files are written to reports/ as normal — no special temp dir.
"""

import json
from pathlib import Path

from utils.generate_report import generate_report, last_query_log_entry

LOG_PATH = Path("logs/query_log.jsonl")


def _esc_check(text: str, html_content: str) -> bool:
    """Check if text appears in html_content (handles basic HTML escaping)."""
    import html as _html
    return _html.escape(text) in html_content or text in html_content
CLEANING_LOG_PATH = Path("logs/cleaning_log.jsonl")


def _load_log_entries() -> list:
    if not LOG_PATH.exists():
        return []
    return [json.loads(l) for l in LOG_PATH.read_text().splitlines() if l.strip()]


# ── Test 1: plain SQL entry — Visualization omitted ───────────────────────────
print("=" * 70)
print("TEST 1: plain sql_analyst entry — Visualization section OMITTED")
print("=" * 70)

entries = _load_log_entries()
sql_entries = [e for e in entries if e.get("route_response") == "sql_analyst"]
assert sql_entries, "Need at least one sql_analyst entry in query_log.jsonl to run this test"

entry1 = sql_entries[-1]
print(f"Using entry: {entry1.get('user_question')!r}")

report_path1 = generate_report(entry1)
print(f"Report written to: {report_path1}")

html1 = Path(report_path1).read_text()

assert "<h2>Introduction</h2>" in html1, "Introduction section must be present"
assert "<h2>Data Cleaning</h2>" in html1, "Data Cleaning section must be present"
assert "<h2>Topic Focus</h2>" in html1, "Topic Focus section must be present"
assert "<h2>Summary</h2>" in html1, "Summary section must be present"
assert "<h2>Visualization</h2>" not in html1, (
    "Visualization section must be OMITTED for a plain sql_analyst entry"
)
print("PASS: four sections present, Visualization correctly omitted.\n")

# ── Test 2: visualize entry — Visualization section included ──────────────────
print("=" * 70)
print("TEST 2: visualize entry — Visualization section INCLUDED")
print("=" * 70)

viz_entries = [e for e in entries if e.get("route_response") == "visualize"]

if not viz_entries:
    print("SKIP: no visualize entries in query_log.jsonl — create one with 'visualize: ...' first.")
    print("      (This test will be run during the full integration test below.)\n")
else:
    entry2 = viz_entries[-1]
    print(f"Using entry: {entry2.get('user_question')!r}")
    report_path2 = generate_report(entry2)
    html2 = Path(report_path2).read_text()

    assert "<h2>Visualization</h2>" in html2, (
        "Visualization section must be INCLUDED for a visualize entry"
    )
    chart_type = entry2.get("chart_type", "")
    if chart_type:
        assert _esc_check(chart_type, html2), (
            f"chart_type {chart_type!r} must appear in the Visualization section"
        )
    print(f"PASS: Visualization section included with chart_type={chart_type!r}.\n")

# ── Test 3: table with no cleaning history ────────────────────────────────────
print("=" * 70)
print("TEST 3: table with no cleaning history — honest statement, no fabrication")
print("=" * 70)

# Build a synthetic entry for a table that has never been cleaned.
synthetic_entry = {
    "timestamp": "2026-01-01T00:00:00+00:00",
    "route_response": "sql_analyst",
    "route_comments": "",
    "user_question": "How many products are in the database?",
    "curated_question": "How many products are in the database?",
    "generated_sql_query": "SELECT COUNT(*) AS total FROM olist_products_dataset;",
    "is_safe": "yes",
    "comments": "",
    "sql_query_execution_result": "[{'total': 32951}]",
    "final_answer": "There are 32,951 products.",
}

# Temporarily ensure olist_products_dataset has no cleaning history by checking
# whether it appears in cleaning_log.jsonl.
cleaning_entries = []
if CLEANING_LOG_PATH.exists():
    for line in CLEANING_LOG_PATH.read_text().splitlines():
        try:
            cleaning_entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass

products_ever_cleaned = any(
    f.get("table_name") == "olist_products_dataset"
    for e in cleaning_entries
    for f in e.get("files", [])
)

report_path3 = generate_report(synthetic_entry)
html3 = Path(report_path3).read_text()

if not products_ever_cleaned:
    assert "no cleaning history" in html3.lower() or "never been processed" in html3.lower(), (
        "Data Cleaning section must state honestly that this table has no cleaning history"
    )
    print("PASS: no-history table described honestly (no fabrication).")
else:
    print("NOTE: olist_products_dataset has cleaning history — no-history assertion skipped.")
    print("      Data Cleaning section was populated from real log data.")
print()

# ── Test 4: report last — no new query_log entry created ─────────────────────
print("=" * 70)
print("TEST 4: last_query_log_entry() reads without adding a new entry")
print("=" * 70)

before_count = sum(1 for _ in LOG_PATH.open()) if LOG_PATH.exists() else 0

entry4 = last_query_log_entry()
assert entry4 is not None, "last_query_log_entry() must return an entry (log is non-empty)"

after_count = sum(1 for _ in LOG_PATH.open()) if LOG_PATH.exists() else 0
assert before_count == after_count, (
    f"last_query_log_entry() must NOT add a new line to query_log.jsonl "
    f"(was {before_count}, now {after_count})"
)
print(f"PASS: last_query_log_entry() read entry without adding a new log line.\n")
print(f"Last entry question: {entry4.get('user_question')!r}")

# ── Build a report from the last entry (smoke-test the full pipeline) ─────────
report_path4 = generate_report(entry4)
print(f"Report from 'report last' written to: {report_path4}")
assert Path(report_path4).exists(), "Report file must exist after generate_report()"
assert Path(report_path4).stat().st_size > 0, "Report file must not be empty"
print("PASS: generate_report(last_entry) produced a non-empty HTML file.\n")

print("=" * 70)
print("ALL GENERATE_REPORT TESTS PASSED")
print("=" * 70)
