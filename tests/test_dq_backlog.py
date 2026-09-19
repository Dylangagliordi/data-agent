"""
Tests for utils/dq_backlog.py (Spec 3: DQ Backlog View).

Requires a live Postgres. No LLM is ever called by this module. Seeds three
temporary rows in _data_quality_status via the admin connection and restores
(deletes) them in a finally block — same seed/restore discipline
test_transformation_options_live_wiring.py already uses — never touching any
real table's actual status.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_dq_backlog.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.dq_backlog as dq_backlog_module
from utils.dq_backlog import get_dq_backlog, render_dq_backlog_html
from utils.load_data import get_admin_connection, write_data_quality_status

FAIL_TABLE = "_test_dq_backlog_fail"
WARN_TABLE = "_test_dq_backlog_warn"
PASS_TABLE = "_test_dq_backlog_pass"

FAIL_ISSUES = [
    {"issue": "Duplicate rows: 3 found", "severity": "fail"},
    {"issue": "Placeholder values: column 'x' (['-1'])", "severity": "fail"},
]
WARN_ISSUES = [
    {"issue": "Missing values: column 'y' (12%)", "severity": "warn"},
]


def _seed():
    conn = get_admin_connection()
    write_data_quality_status(conn, FAIL_TABLE, "fail", FAIL_ISSUES, was_cleaned=True)
    write_data_quality_status(conn, WARN_TABLE, "warn", WARN_ISSUES, was_cleaned=True)
    write_data_quality_status(conn, PASS_TABLE, "pass", [], was_cleaned=True)
    return conn


def _restore(conn):
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM _data_quality_status WHERE table_name IN (%s, %s, %s)",
            (FAIL_TABLE, WARN_TABLE, PASS_TABLE),
        )
    conn.commit()
    conn.close()


def test_backlog_excludes_pass_and_ranks_fail_before_warn():
    conn = _seed()
    try:
        backlog = get_dq_backlog()
        names = [entry["table_name"] for entry in backlog]

        assert PASS_TABLE not in names, "a pass-status table must never appear in the backlog"
        assert FAIL_TABLE in names
        assert WARN_TABLE in names
        assert names.index(FAIL_TABLE) < names.index(WARN_TABLE), (
            "a fail-status table must rank above every warn-status table"
        )

        fail_entry = next(e for e in backlog if e["table_name"] == FAIL_TABLE)
        assert fail_entry["status"] == "fail"
        assert fail_entry["issue_count"] == 2
        assert fail_entry["issues"] == FAIL_ISSUES

        warn_entry = next(e for e in backlog if e["table_name"] == WARN_TABLE)
        assert warn_entry["issue_count"] == 1
        assert warn_entry["issues"] == WARN_ISSUES
        print("PASS: backlog excludes pass-status tables and ranks fail above warn")
    finally:
        _restore(conn)


def test_more_issues_ranks_first_within_the_same_tier():
    conn = get_admin_connection()
    two_issue_table = "_test_dq_backlog_fail_2"
    four_issue_table = "_test_dq_backlog_fail_4"
    try:
        write_data_quality_status(conn, two_issue_table, "fail", FAIL_ISSUES, was_cleaned=True)
        write_data_quality_status(
            conn, four_issue_table, "fail", FAIL_ISSUES + FAIL_ISSUES, was_cleaned=True
        )
        backlog = get_dq_backlog()
        names = [entry["table_name"] for entry in backlog]
        assert names.index(four_issue_table) < names.index(two_issue_table), (
            "more issues in the same status tier must rank first"
        )
        print("PASS: within the same status tier, more issues ranks first")
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM _data_quality_status WHERE table_name IN (%s, %s)",
                (two_issue_table, four_issue_table),
            )
        conn.commit()
        conn.close()


def test_render_dq_backlog_html_writes_real_file_with_issues():
    conn = _seed()
    try:
        path = render_dq_backlog_html()
        assert os.path.isfile(path)
        content = open(path, encoding="utf-8").read()
        assert FAIL_TABLE in content
        assert "Duplicate rows: 3 found" in content
        assert PASS_TABLE not in content
        print(f"PASS: render_dq_backlog_html wrote a real file with real issue text at {path}")
    finally:
        _restore(conn)


def test_render_dq_backlog_html_empty_case_is_isolated_from_real_db_state():
    """Tested via monkeypatch rather than by clearing every real row in the
    live database — this project's shared Postgres may legitimately have
    other tables at fail/warn status, and this test has no business
    asserting anything about that global state."""
    original_get_backlog = dq_backlog_module.get_dq_backlog
    dq_backlog_module.get_dq_backlog = lambda: []
    try:
        empty_path = dq_backlog_module.render_dq_backlog_html()
        empty_content = open(empty_path, encoding="utf-8").read()
        assert "No outstanding data-quality issues" in empty_content
        print("PASS: an empty backlog renders an honest message, not a blank page")
    finally:
        dq_backlog_module.get_dq_backlog = original_get_backlog


if __name__ == "__main__":
    test_backlog_excludes_pass_and_ranks_fail_before_warn()
    test_more_issues_ranks_first_within_the_same_tier()
    test_render_dq_backlog_html_writes_real_file_with_issues()
    test_render_dq_backlog_html_empty_case_is_isolated_from_real_db_state()
    print("\nAll dq_backlog tests passed.")
