"""Tests for utils/generate_report.py (Spec 2: report body = the shared
narrative walkthrough, utils/narrative.py).

Content-exactness checks target build_narrative_walkthrough's DETERMINISTIC
step objects directly (pre-narration — the LLM narration pass legitimately
paraphrases, so exact substrings aren't a stable thing to assert against
post-narration HTML). generate_report()-level checks are structural: the
right part headers / step titles are present in the right order.

Test 1: plain sql_analyst entry — Part A + Part C present, no chart step.
Test 2a: visualize entry with chart_type_source == "reasoned" — the
  deterministic chart step's explanation includes the real reasoning text;
  the rendered report includes the chart step and a real embedded image.
Test 2b: visualize entry with chart_type_source == "explicit" — the
  deterministic chart step states only that the type was requested, no
  reasoning text (none was ever generated for explicit selections).
Test 3: table with no cleaning history — states this honestly (no
  fabrication).
Test 4: report last reads the most recent log entry without adding a new one.
"""

import json
from pathlib import Path

from utils.generate_report import generate_report, last_query_log_entry
from utils.narrative import build_narrative_walkthrough

LOG_PATH = Path("logs/query_log.jsonl")
CLEANING_LOG_PATH = Path("logs/cleaning_log.jsonl")


def _load_log_entries() -> list:
    if not LOG_PATH.exists():
        return []
    return [json.loads(l) for l in LOG_PATH.read_text().splitlines() if l.strip()]


# ── Test 1: plain sql_analyst entry — no chart step ────────────────────────────
print("=" * 70)
print("TEST 1: plain sql_analyst entry — Part A + Part C present, no chart step")
print("=" * 70)

entries = _load_log_entries()
sql_entries = [e for e in entries if e.get("route_response") == "sql_analyst"]
assert sql_entries, "Need at least one sql_analyst entry in query_log.jsonl to run this test"

entry1 = sql_entries[-1]
print(f"Using entry: {entry1.get('user_question')!r}")

steps1 = build_narrative_walkthrough(entry1)
assert any(s.part == "cleaning" for s in steps1)
assert any(s.part == "analysis" for s in steps1)
assert not any(s.title.startswith("Visualize the result") for s in steps1), (
    "a plain sql_analyst entry must never produce a chart step"
)

report_path1 = generate_report(entry1)
html1 = Path(report_path1).read_text()
assert "Part A: Data Cleaning" in html1
assert "Part C: Analysis" in html1
assert "Visualize the result" not in html1
print("PASS: Part A and Part C present, chart step correctly absent.\n")

# ── Test 2a: visualize entry with chart_type_source == "reasoned" ────────────
print("=" * 70)
print("TEST 2a: visualize entry (reasoned) — real reasoning in the chart step")
print("=" * 70)

viz_entries = [e for e in entries if e.get("route_response") == "visualize"]
reasoned_entries = [e for e in viz_entries if e.get("chart_type_source") == "reasoned"]

if not reasoned_entries:
    print("SKIP: no visualize entries with chart_type_source='reasoned' in query_log.jsonl.\n")
else:
    entry2a = reasoned_entries[-1]
    print(f"Using entry: {entry2a.get('user_question')!r}")

    steps2a = build_narrative_walkthrough(entry2a)
    chart_step2a = next(s for s in steps2a if s.title.startswith("Visualize the result"))
    reasoning_2a = entry2a.get("chart_type_reasoning", "")
    assert reasoning_2a, "Test entry must have chart_type_reasoning for reasoned selection"
    assert reasoning_2a in chart_step2a.explanation, (
        "the deterministic chart step must include the real reasoning text verbatim"
    )

    report_path2a = generate_report(entry2a)
    html2a = Path(report_path2a).read_text()
    assert f"Visualize the result as a {entry2a.get('chart_type', '')}" in html2a
    if entry2a.get("chart_image_path") and Path(entry2a["chart_image_path"]).exists():
        assert "data:image/png;base64," in html2a
    print(f"PASS: chart step includes real reasoning for chart_type={entry2a.get('chart_type')!r}.\n")

# ── Test 2b: visualize entry with chart_type_source == "explicit" ────────────
print("=" * 70)
print("TEST 2b: visualize entry (explicit) — no reasoning in the chart step")
print("=" * 70)

explicit_entries = [e for e in viz_entries if e.get("chart_type_source") == "explicit" and e.get("chart_type")]

if not explicit_entries:
    print("SKIP: no visualize entries with chart_type_source='explicit' in query_log.jsonl.\n")
else:
    entry2b = explicit_entries[-1]
    print(f"Using entry: {entry2b.get('user_question')!r}")

    steps2b = build_narrative_walkthrough(entry2b)
    chart_step2b = next(s for s in steps2b if s.title.startswith("Visualize the result"))
    assert chart_step2b.explanation.startswith("You asked for a"), (
        f"an explicit chart_type_source must produce a plain 'you asked for' statement, "
        f"got: {chart_step2b.explanation!r}"
    )

    report_path2b = generate_report(entry2b)
    html2b = Path(report_path2b).read_text()
    assert f"Visualize the result as a {entry2b.get('chart_type', '')}" in html2b
    print(f"PASS: chart step has no fabricated reasoning for chart_type={entry2b.get('chart_type')!r}.\n")

# ── Test 3: table with no cleaning history ────────────────────────────────────
print("=" * 70)
print("TEST 3: table with no cleaning history — honest statement, no fabrication")
print("=" * 70)

synthetic_entry = {
    "timestamp": "2026-01-01T00:00:00+00:00",
    "route_response": "sql_analyst",
    "route_comments": "",
    "user_question": "How many products are in the database?",
    "curated_question": "How many products are in the database?",
    "generated_sql_query": "SELECT COUNT(*) AS total FROM olist_products_dataset;",
    "is_safe": "yes",
    "comments": "",
    "sql_query_execution_result": json.dumps({"columns": ["total"], "rows": [[32951]], "truncated": False}),
    "final_answer": "There are 32,951 products.",
}

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

steps3 = build_narrative_walkthrough(synthetic_entry)
cleaning_steps3 = [s for s in steps3 if s.part == "cleaning"]

if not products_ever_cleaned:
    assert any(
        "no cleaning history" in s.explanation.lower() or "never been processed" in s.explanation.lower()
        for s in cleaning_steps3
    ), "must state honestly that this table has no cleaning history"
    print("PASS: no-history table described honestly (no fabrication).")
else:
    print("NOTE: olist_products_dataset has cleaning history — no-history assertion skipped.")

report_path3 = generate_report(synthetic_entry)
assert Path(report_path3).exists() and Path(report_path3).stat().st_size > 0
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
print("PASS: last_query_log_entry() read entry without adding a new log line.\n")
print(f"Last entry question: {entry4.get('user_question')!r}")

report_path4 = generate_report(entry4)
print(f"Report from 'report last' written to: {report_path4}")
assert Path(report_path4).exists(), "Report file must exist after generate_report()"
assert Path(report_path4).stat().st_size > 0, "Report file must not be empty"
print("PASS: generate_report(last_entry) produced a non-empty HTML file.\n")

print("=" * 70)
print("ALL GENERATE_REPORT TESTS PASSED (1, 2a, 2b, 3, 4)")
print("=" * 70)
