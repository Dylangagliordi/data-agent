"""
Tests for Spec 8, Part 5: Full Audit Export (utils/audit_export.py).

Pure file-based (monkeypatches CLEANING_LOG_PATH/QUERY_LOG_PATH/OUTPUT_DIR to
tmp files/dirs) — no DB, no LLM, and never touches the project's real logs.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_audit_export.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.audit_export as ae


def _write_jsonl(path: Path, entries: list):
    with open(path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


def test_get_table_audit_history_case_insensitive_and_cross_log():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cleaning_log = Path(tmp_dir) / "cleaning_log.jsonl"
        query_log = Path(tmp_dir) / "query_log.jsonl"

        _write_jsonl(
            cleaning_log,
            [
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "trigger": "manual",
                    "files": [
                        {
                            "file_name": "Uncleaned_DS_jobs.csv",
                            "table_name": "Uncleaned_DS_jobs",  # mixed case, like the real log
                            "status": "cleaned",
                            "row_count_before": 672,
                            "row_count_after": 672,
                            "issues_found": [{"issue": "Placeholder values: -1", "severity": "fail"}],
                            "issues_resolved": ["Placeholder values: -1"],
                            "issues_still_unresolved": [],
                        },
                        {
                            "file_name": "other_table.csv",
                            "table_name": "other_table",
                            "status": "cleaned",
                            "row_count_before": 10,
                            "row_count_after": 10,
                            "issues_found": [],
                            "issues_resolved": [],
                            "issues_still_unresolved": [],
                        },
                    ],
                }
            ],
        )
        _write_jsonl(
            query_log,
            [
                {
                    "timestamp": "2026-01-02T00:00:00+00:00",
                    "route_response": "sql_analyst",
                    "user_question": "what industries are in the data",
                    "transformation_narrative_log": [
                        {
                            "table_name": "uncleaned_ds_jobs",  # lowercase this time
                            "candidate": {"kind": "categorical_consolidation"},
                            "chosen_option_id": "consolidate",
                            "fresh": True,
                            "reload_reask": False,
                        }
                    ],
                },
                {
                    "timestamp": "2026-01-03T00:00:00+00:00",
                    "route_response": "sql_analyst",
                    "user_question": "unrelated question about other_table",
                    "transformation_narrative_log": [
                        {
                            "table_name": "other_table",
                            "candidate": {"kind": "feature_derivation"},
                            "chosen_option_id": "derive",
                            "fresh": False,
                            "reload_reask": False,
                        }
                    ],
                },
            ],
        )

        original_cleaning, original_query = ae.CLEANING_LOG_PATH, ae.QUERY_LOG_PATH
        ae.CLEANING_LOG_PATH, ae.QUERY_LOG_PATH = cleaning_log, query_log
        try:
            history = ae.get_table_audit_history("uncleaned_DS_jobs")  # yet another casing
            assert len(history["cleaning_events"]) == 1, "must match case-insensitively and exclude other_table"
            assert history["cleaning_events"][0]["file_name"] == "Uncleaned_DS_jobs.csv"
            assert len(history["transformation_decisions"]) == 1, "must match across casing and exclude other_table"
            assert history["transformation_decisions"][0]["candidate_kind"] == "categorical_consolidation"
            print("PASS: get_table_audit_history matches case-insensitively and never mixes in another table's events")
        finally:
            ae.CLEANING_LOG_PATH, ae.QUERY_LOG_PATH = original_cleaning, original_query


def test_get_table_audit_history_empty():
    with tempfile.TemporaryDirectory() as tmp_dir:
        original_cleaning, original_query = ae.CLEANING_LOG_PATH, ae.QUERY_LOG_PATH
        ae.CLEANING_LOG_PATH = Path(tmp_dir) / "nonexistent_cleaning.jsonl"
        ae.QUERY_LOG_PATH = Path(tmp_dir) / "nonexistent_query.jsonl"
        try:
            history = ae.get_table_audit_history("never_seen_table")
            assert history["cleaning_events"] == []
            assert history["transformation_decisions"] == []
            print("PASS: get_table_audit_history returns empty lists, never an error, for an untracked table")
        finally:
            ae.CLEANING_LOG_PATH, ae.QUERY_LOG_PATH = original_cleaning, original_query


def test_render_table_audit_html():
    with tempfile.TemporaryDirectory() as tmp_dir:
        cleaning_log = Path(tmp_dir) / "cleaning_log.jsonl"
        query_log = Path(tmp_dir) / "query_log.jsonl"
        out_dir = Path(tmp_dir) / "out"
        _write_jsonl(
            cleaning_log,
            [
                {
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "trigger": "manual",
                    "files": [
                        {
                            "file_name": "orders.csv",
                            "table_name": "orders",
                            "status": "cleaned",
                            "row_count_before": 100,
                            "row_count_after": 98,
                            "issues_found": [],
                            "issues_resolved": ["Duplicate rows: 2"],
                            "issues_still_unresolved": [],
                        }
                    ],
                }
            ],
        )
        _write_jsonl(query_log, [])

        original_cleaning, original_query, original_out = ae.CLEANING_LOG_PATH, ae.QUERY_LOG_PATH, ae.OUTPUT_DIR
        ae.CLEANING_LOG_PATH, ae.QUERY_LOG_PATH, ae.OUTPUT_DIR = cleaning_log, query_log, out_dir
        try:
            path = ae.render_table_audit_html("orders")
            content = Path(path).read_text()
            assert "orders.csv" in content
            assert "100" in content and "98" in content
            assert "No transformation decisions recorded" in content
            print("PASS: render_table_audit_html renders a real single-event timeline")
        finally:
            ae.CLEANING_LOG_PATH, ae.QUERY_LOG_PATH, ae.OUTPUT_DIR = original_cleaning, original_query, original_out


if __name__ == "__main__":
    test_get_table_audit_history_case_insensitive_and_cross_log()
    test_get_table_audit_history_empty()
    test_render_table_audit_html()
    print("\nAll audit_export tests passed.")
