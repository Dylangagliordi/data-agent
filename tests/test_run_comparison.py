"""
Tests for utils/run_comparison.py (Spec 4: Run Comparison / Diff).

No database, no LLM, no live network — pure reads over a temporary,
hand-written query_log.jsonl (LOG_PATH monkeypatched to a temp file for the
whole run, restored at the end).

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_run_comparison.py
"""

import json
import os
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.run_comparison as run_comparison_module
from utils.run_comparison import compare_runs, render_run_comparison_html

QUESTION = "which industries pay the most for data scientists"


def _result_json(columns, rows):
    return json.dumps({"columns": columns, "rows": rows, "truncated": False})


def _write_log(entries):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")
    return path


def _use_temp_log(entries):
    path = _write_log(entries)
    original_log_path = run_comparison_module.LOG_PATH
    run_comparison_module.LOG_PATH = type(original_log_path)(path)
    return original_log_path, path


def test_changed_sql_and_rows_are_detected():
    original, temp_path = _use_temp_log([
        {
            "timestamp": "2026-01-01T00:00:00Z", "route_response": "sql_analyst",
            "user_question": QUESTION,
            "generated_sql_query": "SELECT industry, AVG(salary) FROM jobs GROUP BY industry",
            "sql_query_execution_result": _result_json(["industry", "avg_salary"], [["Finance", 120000]]),
            "final_answer": "Finance pays the most.",
        },
        {
            "timestamp": "2026-02-01T00:00:00Z", "route_response": "sql_analyst",
            "user_question": QUESTION,
            "generated_sql_query": "SELECT industry, AVG(salary) FROM jobs GROUP BY industry HAVING COUNT(*) >= 5",
            "sql_query_execution_result": _result_json(["industry", "avg_salary"], [["Tech", 125000]]),
            "final_answer": "Tech pays the most.",
        },
    ])
    try:
        diff = compare_runs(QUESTION)
        assert diff is not None
        assert diff["sql_changed"] is True
        assert diff["rows_identical"] is False
        assert diff["row_count_before"] == 1
        assert diff["row_count_after"] == 1
        assert diff["final_answer_changed"] is True
        assert diff["earlier_final_answer"] == "Finance pays the most."
        assert diff["later_final_answer"] == "Tech pays the most."
        print("PASS: a genuinely changed SQL query and result set are correctly detected")
    finally:
        run_comparison_module.LOG_PATH = original
        os.remove(temp_path)


def test_identical_runs_report_no_change():
    entry = {
        "timestamp": "2026-01-01T00:00:00Z", "route_response": "sql_analyst",
        "user_question": QUESTION,
        "generated_sql_query": "SELECT industry FROM jobs",
        "sql_query_execution_result": _result_json(["industry"], [["Finance"]]),
        "final_answer": "Finance.",
    }
    original, temp_path = _use_temp_log([dict(entry, timestamp="2026-01-01T00:00:00Z"),
                                          dict(entry, timestamp="2026-02-01T00:00:00Z")])
    try:
        diff = compare_runs(QUESTION)
        assert diff["sql_changed"] is False
        assert diff["rows_identical"] is True
        assert diff["final_answer_changed"] is False
        print("PASS: two identical runs correctly report no change")
    finally:
        run_comparison_module.LOG_PATH = original
        os.remove(temp_path)


def test_fewer_than_two_matches_returns_none():
    original, temp_path = _use_temp_log([
        {"timestamp": "2026-01-01T00:00:00Z", "route_response": "sql_analyst",
         "user_question": QUESTION, "final_answer": "only one run so far"},
    ])
    try:
        assert compare_runs(QUESTION) is None
        assert render_run_comparison_html(QUESTION) is None
        print("PASS: fewer than 2 matching runs returns None, not a crash or a broken file")
    finally:
        run_comparison_module.LOG_PATH = original
        os.remove(temp_path)


def test_two_etl_runs_report_sql_fields_as_not_applicable():
    original, temp_path = _use_temp_log([
        {"timestamp": "2026-01-01T00:00:00Z", "route_response": "etl_analyst",
         "user_question": "download the jobs dataset", "final_answer": "Downloaded 1 file."},
        {"timestamp": "2026-02-01T00:00:00Z", "route_response": "etl_analyst",
         "user_question": "download the jobs dataset", "final_answer": "Downloaded 1 file (refreshed)."},
    ])
    try:
        diff = compare_runs("download the jobs dataset")
        assert diff["sql_changed"] is None, "two ETL runs have no SQL to compare — must not be fabricated to False"
        assert diff["rows_identical"] is None
        assert diff["columns_changed"] is None
        assert diff["final_answer_changed"] is True
        print("PASS: two ETL-analyst runs report sql/rows as not-applicable, never a fabricated False")
    finally:
        run_comparison_module.LOG_PATH = original
        os.remove(temp_path)


def test_render_writes_a_real_file_when_there_is_something_to_compare():
    entry = {
        "timestamp": "2026-01-01T00:00:00Z", "route_response": "sql_analyst",
        "user_question": QUESTION,
        "generated_sql_query": "SELECT industry FROM jobs",
        "sql_query_execution_result": _result_json(["industry"], [["Finance"]]),
        "final_answer": "Finance.",
    }
    original, temp_path = _use_temp_log([dict(entry, timestamp="2026-01-01T00:00:00Z"),
                                          dict(entry, timestamp="2026-02-01T00:00:00Z", final_answer="Tech.")])
    try:
        path = render_run_comparison_html(QUESTION)
        assert path is not None and os.path.isfile(path)
        content = open(path, encoding="utf-8").read()
        assert "Finance." in content and "Tech." in content
        print(f"PASS: render_run_comparison_html wrote a real file at {path}")
    finally:
        run_comparison_module.LOG_PATH = original
        os.remove(temp_path)


if __name__ == "__main__":
    test_changed_sql_and_rows_are_detected()
    test_identical_runs_report_no_change()
    test_fewer_than_two_matches_returns_none()
    test_two_etl_runs_report_sql_fields_as_not_applicable()
    test_render_writes_a_real_file_when_there_is_something_to_compare()
    print("\nAll run_comparison tests passed.")
