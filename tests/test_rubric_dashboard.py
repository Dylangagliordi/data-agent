"""
Tests for Spec 8, Part 3: Rubric Rules Dashboard (utils/rubric_dashboard.py).

Verifies detection against REAL disclosure text produced by
agents/sql_analyst.py's own rubric functions (_analyst_judgment_disclosure,
_apply_causal_correction) — never hand-guessed substrings — so a future
wording change in those functions would break this test rather than silently
under/over-count.

Pure file-based (monkeypatches LOG_PATH to a tmp file) — no DB, no LLM.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_rubric_dashboard.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.rubric_dashboard as rd
from agents.sql_analyst import _analyst_judgment_disclosure, _apply_causal_correction


def _write_entries(path: Path, entries: list):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_detectors_match_the_real_disclosure_functions_output():
    # Real SQL that trips Rule 1 (min sample), Rule 2 (combined ranking),
    # Rule 3 (null exclusion), Rule 12 (dedup) all at once.
    sql = (
        "SELECT DISTINCT industry, AVG(rating) AS avg_rating, COUNT(*) AS n "
        "FROM jobs WHERE industry IS NOT NULL GROUP BY industry "
        "HAVING COUNT(*) >= 5 ORDER BY avg_rating DESC, n ASC"
    )
    disclosure = _analyst_judgment_disclosure(sql, result_data=None)
    assert rd._detect_min_sample(disclosure)
    assert rd._detect_combined_ranking(disclosure)
    assert rd._detect_null_exclusion(disclosure)
    assert rd._detect_deduplication(disclosure)
    assert not rd._detect_time_framing(disclosure)
    assert not rd._detect_outlier_sensitivity(disclosure)
    assert not rd._detect_group_size_imbalance(disclosure)

    time_sql = "SELECT * FROM orders WHERE order_date BETWEEN '2026-01-01' AND '2026-02-01'"
    time_disclosure = _analyst_judgment_disclosure(time_sql, result_data=None)
    assert rd._detect_time_framing(time_disclosure)

    causal_answer = _apply_causal_correction("Higher marketing spend drives higher revenue.", "")
    assert rd._detect_causal_correction(causal_answer)
    print("PASS: each detector matches the real, live-generated disclosure text for its rule")


def test_get_rubric_dashboard_counts_and_rates():
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_path = Path(tmp_dir) / "query_log.jsonl"
        entries = [
            {
                "route_response": "sql_analyst",
                "final_answer": "5 rows.\n\nHow this answer was computed: This result is ranked primarily by Avg Rating (descending).",
            },
            {
                "route_response": "sql_analyst",
                "final_answer": "Plain answer, no rubric rule fired.",
            },
            {
                "route_response": "visualize",
                "final_answer": "Chart built.\n\nHow this answer was computed: Duplicate rows were removed from this result (SELECT DISTINCT was used).",
            },
            {
                "route_response": "etl_analyst",
                "final_answer": "This result is ranked primarily by should not be counted (wrong route).",
            },
        ]
        _write_entries(log_path, entries)

        original = rd.LOG_PATH
        rd.LOG_PATH = log_path
        try:
            dashboard = rd.get_rubric_dashboard()
            assert dashboard["total_eligible_runs"] == 3, "etl_analyst entries must be excluded"
            by_number = {r["number"]: r for r in dashboard["rules"]}
            assert by_number[2]["fire_count"] == 1
            assert by_number[2]["fire_rate"] == 1 / 3
            assert by_number[12]["fire_count"] == 1
            assert by_number[1]["fire_count"] == 0
            print("PASS: get_rubric_dashboard counts only eligible runs and computes correct rates")
        finally:
            rd.LOG_PATH = original


def test_get_rubric_dashboard_empty_log():
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_path = Path(tmp_dir) / "query_log.jsonl"  # never created
        original = rd.LOG_PATH
        rd.LOG_PATH = log_path
        try:
            dashboard = rd.get_rubric_dashboard()
            assert dashboard["total_eligible_runs"] == 0
            assert all(r["fire_rate"] == 0.0 for r in dashboard["rules"])
            print("PASS: get_rubric_dashboard handles a missing log file with zero rates, no division error")
        finally:
            rd.LOG_PATH = original


def test_render_rubric_dashboard_html():
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_path = Path(tmp_dir) / "query_log.jsonl"
        out_dir = Path(tmp_dir) / "out"
        _write_entries(
            log_path,
            [{"route_response": "sql_analyst", "final_answer": "This result is ranked primarily by X."}],
        )
        original_log, original_out = rd.LOG_PATH, rd.OUTPUT_DIR
        rd.LOG_PATH = log_path
        rd.OUTPUT_DIR = out_dir
        try:
            path = rd.render_rubric_dashboard_html()
            content = Path(path).read_text()
            assert "Combined-metric ranking transparency" in content
            assert "Fan-out / grain safety" in content  # the not-observable table
            assert "1 / 1" in content
            print("PASS: render_rubric_dashboard_html renders real counts and the not-observable rules note")
        finally:
            rd.LOG_PATH, rd.OUTPUT_DIR = original_log, original_out


if __name__ == "__main__":
    test_detectors_match_the_real_disclosure_functions_output()
    test_get_rubric_dashboard_counts_and_rates()
    test_get_rubric_dashboard_empty_log()
    test_render_rubric_dashboard_html()
    print("\nAll rubric_dashboard tests passed.")
