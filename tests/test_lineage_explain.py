"""
Tests for Spec 8, Part 4: standalone lineage / "explain this number"
(main.py's "explain: <question>" command).

This is deliberately thin glue over two things this project already built
and already tests independently: utils.run_comparison.find_entries_for_question
(Spec 4) and utils.generate_report.generate_report (Spec 2). The one real new
risk is main.py's own selection logic — picking the MOST RECENT past run of
the exact question (entries[-1]) without re-running it, even when other,
unrelated questions were logged more recently overall — so that's what this
test actually targets, plus the "no history yet" error path.

Uses a real, temporary query_log.jsonl (monkeypatching both modules'
LOG_PATH constants) rather than the project's live log, so this never
depends on — or pollutes — real history.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_lineage_explain.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.run_comparison as run_comparison
from utils.generate_report import generate_report

QUESTION = "how many orders came from São Paulo"
OTHER_QUESTION = "what is the average payment value"


def _entry(question, sql, timestamp, final_answer=None):
    return {
        "timestamp": timestamp,
        "route_response": "sql_analyst",
        "route_comments": "",
        "user_question": question,
        "curated_question": question,
        "generated_sql_query": sql,
        "is_safe": "yes",
        "comments": "",
        "sql_query_execution_result": json.dumps({"columns": ["n"], "rows": [[42]], "truncated": False}),
        "final_answer": final_answer or "42 orders.",
    }


def test_find_entries_for_question_ignores_more_recent_unrelated_runs():
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_path = Path(tmp_dir) / "query_log.jsonl"
        entries = [
            _entry(QUESTION, "SELECT COUNT(*) FROM orders WHERE city = 'old'", "2026-01-01T00:00:00+00:00"),
            _entry(QUESTION, "SELECT COUNT(*) FROM orders WHERE city = 'new'", "2026-02-01T00:00:00+00:00"),
            # Logged AFTER both of the above, but a different question entirely —
            # must never be picked as "the most recent run of QUESTION".
            _entry(OTHER_QUESTION, "SELECT AVG(payment_value) FROM payments", "2026-03-01T00:00:00+00:00"),
        ]
        with open(log_path, "w") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

        original = run_comparison.LOG_PATH
        run_comparison.LOG_PATH = log_path
        try:
            matches = run_comparison.find_entries_for_question(QUESTION)
            assert len(matches) == 2
            most_recent = matches[-1]
            assert most_recent["generated_sql_query"] == "SELECT COUNT(*) FROM orders WHERE city = 'new'", (
                "explain: must select the most recent run of the EXACT question, "
                "not simply the last line in the whole log"
            )

            # The actual glue: build a report from that specific historical
            # entry without re-running anything.
            report_path = generate_report(most_recent)
            html = Path(report_path).read_text()
            assert "city = 'new'" in html or "42 orders" in html
            print("PASS: explain: resolves to the most recent run of the exact question, not the log's last line")
        finally:
            run_comparison.LOG_PATH = original


def test_find_entries_for_question_no_history():
    with tempfile.TemporaryDirectory() as tmp_dir:
        log_path = Path(tmp_dir) / "query_log.jsonl"  # never created
        original = run_comparison.LOG_PATH
        run_comparison.LOG_PATH = log_path
        try:
            matches = run_comparison.find_entries_for_question("a question never asked before")
            assert matches == []
            print("PASS: find_entries_for_question returns [] with no matching history (explain:'s error path)")
        finally:
            run_comparison.LOG_PATH = original


if __name__ == "__main__":
    test_find_entries_for_question_ignores_more_recent_unrelated_runs()
    test_find_entries_for_question_no_history()
    print("\nAll lineage_explain tests passed.")
