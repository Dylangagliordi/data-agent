"""Tests for the optional Tableau .hyper export in build_visualization.

Four scenarios per spec:
1. No tool mentioned -> export_target stays "csv", only CSV written, no .hyper.
2. Tableau explicitly mentioned -> export_target="tableau", BOTH files written.
3. Hyper file is genuinely valid (opens and can be queried via hyper-api).
4. final_answer correctly names which files were produced in each case.
5. Re-run existing test suite (done separately via regression tests).

Runs against synthetic data — no live DB needed for nodes 1-4 of this test.
"""

import csv
from pathlib import Path

from agents.sql_analyst import _detect_export_target, build_visualization, determine_chart_type
from models.schema import SQLAnalystState

FAKE_RESULT = str([
    {"customer_state": "SP", "order_count": 41746},
    {"customer_state": "RJ", "order_count": 12852},
    {"customer_state": "MG", "order_count": 11635},
])

# ── Unit test: export-target detection ────────────────────────────────────────
print("=" * 70)
print("UNIT: _detect_export_target")
print("=" * 70)
assert _detect_export_target("show me a bar chart for Tableau") == "tableau"
assert _detect_export_target("Tableau-ready export please") == "tableau"
assert _detect_export_target("as a Tableau file") == "tableau"
assert _detect_export_target("show me a bar chart") == "csv"
assert _detect_export_target("Power BI dashboard") == "csv"
assert _detect_export_target("Looker Studio visualization") == "csv"
assert _detect_export_target("Metabase chart") == "csv"
assert _detect_export_target("tabulated results by state") == "csv"  # not a match
print("PASS: all detection assertions correct.\n")

# ── Test 1: No tool mentioned -> CSV only ─────────────────────────────────────
print("=" * 70)
print("TEST 1: no tool mentioned -> export_target='csv', CSV only, no .hyper")
print("=" * 70)
state_csv = SQLAnalystState(
    wants_visualization=True,
    user_question="Show the number of orders per customer state.",
    curated_question="Show the number of orders per customer state.",
    chart_type="bar chart",
    chart_type_source="explicit",
    chart_type_reasoning="",
    export_target="csv",
    sql_query_execution_result=FAKE_RESULT,
)
result1 = build_visualization(state_csv)
print("output_file_path:", result1["output_file_path"])
print("final_answer:\n", result1["final_answer"])
print()

csv_path1 = Path(result1["output_file_path"])
hyper_path1 = csv_path1.with_suffix(".hyper")

assert csv_path1.exists(), f"CSV must exist at {csv_path1}"
assert not hyper_path1.exists(), f".hyper must NOT exist when export_target='csv' (found {hyper_path1})"
assert "Visualization data saved to:" in result1["final_answer"], (
    "final_answer must name the CSV file"
)
assert ".hyper" not in result1["final_answer"], (
    "final_answer must NOT mention a .hyper file when export_target='csv'"
)
print(f"PASS: only CSV written, no .hyper file, final_answer correct.\n")

# ── Test 2: Tableau mentioned -> BOTH files written ───────────────────────────
print("=" * 70)
print("TEST 2: Tableau explicitly mentioned -> export_target='tableau', CSV + .hyper")
print("=" * 70)
state_tab = SQLAnalystState(
    wants_visualization=True,
    user_question="Show the number of orders per customer state, Tableau-ready.",
    curated_question="Show the number of orders per customer state in a format ready for Tableau.",
    chart_type="bar chart",
    chart_type_source="explicit",
    chart_type_reasoning="",
    export_target="tableau",
    sql_query_execution_result=FAKE_RESULT,
)
result2 = build_visualization(state_tab)
print("final_answer:\n", result2["final_answer"])
print()

csv_path2 = Path(result2["output_file_path"])
hyper_path2 = csv_path2.with_suffix(".hyper")

assert csv_path2.exists(), f"CSV must exist at {csv_path2}"
assert hyper_path2.exists(), f".hyper must exist at {hyper_path2}"
print(f"CSV size:   {csv_path2.stat().st_size} bytes")
print(f"Hyper size: {hyper_path2.stat().st_size} bytes")
assert hyper_path2.stat().st_size > 0, ".hyper file must not be empty"
print(f"PASS: both CSV and .hyper files written.\n")

# ── Test 3: Hyper file is genuinely valid ─────────────────────────────────────
print("=" * 70)
print("TEST 3: .hyper file is genuinely valid (readable via hyper-api)")
print("=" * 70)
from tableauhyperapi import Connection, HyperProcess, Telemetry

with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
    with Connection(hyper.endpoint, str(hyper_path2)) as conn:
        # Read back the rows from the Extract.Extract table
        rows = conn.execute_list_query("SELECT * FROM \"Extract\".\"Extract\"")
        print(f"Rows in .hyper: {rows}")
        assert len(rows) == 3, f"expected 3 rows, got {len(rows)}"
        # Check values round-tripped correctly
        states = [r[0] for r in rows]
        counts = [r[1] for r in rows]
        print(f"States: {states}, counts: {counts}")
        assert "SP" in states, "SP must be in the hyper file"
        assert 41746 in counts, "41746 must be in the hyper file"
print("PASS: .hyper file is genuinely valid and contains the expected data.\n")

# ── Test 4: final_answer names exactly which files were produced ──────────────
print("=" * 70)
print("TEST 4: final_answer correctly names files in each case")
print("=" * 70)

# CSV-only case (result1)
assert "Files produced:" not in result1["final_answer"], (
    "CSV-only final_answer must NOT have 'Files produced:' block"
)
assert str(csv_path1) in result1["final_answer"], (
    "CSV-only final_answer must include the CSV path"
)

# Tableau case (result2)
assert "Files produced:" in result2["final_answer"], (
    "Tableau final_answer must have 'Files produced:' block"
)
assert "CSV:" in result2["final_answer"], (
    "Tableau final_answer must name the CSV file"
)
assert "Hyper:" in result2["final_answer"], (
    "Tableau final_answer must name the .hyper file"
)
assert str(csv_path2) in result2["final_answer"], "CSV path must be in Tableau final_answer"
assert str(hyper_path2) in result2["final_answer"], ".hyper path must be in Tableau final_answer"
print("PASS: final_answer correctly names files in both cases.\n")

print("=" * 70)
print("ALL TABLEAU EXPORT ASSERTIONS PASSED")
print("=" * 70)
