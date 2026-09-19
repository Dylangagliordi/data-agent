"""
Run comparison / diff (Spec 4): compares the two most recent runs of the
exact same question, read straight from logs/query_log.jsonl. No database,
no LLM — pure file reads over history this project already keeps.

Matching is on the raw, exact user_question string (not curated_question,
which ETL-analyst entries don't even have). Only the two most recent matches
are compared — not an arbitrary pair, not a full timeline.

Deliberately does NOT attempt cell-level "this value changed from X to Y"
diffing: that would require guessing which column is the meaningful key to
match old rows to new ones, and a plain SQL Analyst log entry (unlike a
visualize entry) doesn't record which column that is. Reporting "the rows
changed" honestly is fine; pretending to know how without evidence isn't.

CLI trigger: `python main.py "compare: <question>"`.
See tests/test_run_comparison.py.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from agents.sql_analyst import _parse_sql_result

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "logs" / "query_log.jsonl"
OUTPUT_DIR = "run_comparisons"


def _read_all_log_entries() -> list:
    """Every entry in query_log.jsonl, in the order they were originally
    logged (oldest first). Returns [] if the log doesn't exist yet."""
    if not LOG_PATH.exists():
        return []
    entries = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            entries.append(json.loads(line))
    return entries


def find_entries_for_question(user_question: str) -> list:
    """Every past log entry whose real, raw user_question exactly matches
    user_question, oldest first."""
    return [e for e in _read_all_log_entries() if e.get("user_question") == user_question]


def compare_runs(user_question: str) -> "dict | None":
    """Diff the two most recent runs of user_question. Returns None if fewer
    than 2 matching runs exist — there's nothing to compare yet."""
    entries = find_entries_for_question(user_question)
    if len(entries) < 2:
        return None
    earlier, later = entries[-2], entries[-1]

    earlier_sql = earlier.get("generated_sql_query")
    later_sql = later.get("generated_sql_query")
    if earlier_sql is None and later_sql is None:
        # Neither run had a SQL query at all (e.g. both were ETL-analyst
        # runs) — "unchanged" would misleadingly imply there was something
        # to compare in the first place.
        sql_changed = None
        rows_identical = None
        columns_changed = None
        row_count_before = None
        row_count_after = None
        earlier_rows, later_rows = [], []
    else:
        sql_changed = earlier_sql != later_sql
        earlier_rows, _ = _parse_sql_result(earlier.get("sql_query_execution_result", "") or "")
        later_rows, _ = _parse_sql_result(later.get("sql_query_execution_result", "") or "")

        def _row_key(row):
            return tuple(sorted(row.items()))

        earlier_columns = sorted(earlier_rows[0].keys()) if earlier_rows else []
        later_columns = sorted(later_rows[0].keys()) if later_rows else []
        columns_changed = earlier_columns != later_columns
        row_count_before = len(earlier_rows)
        row_count_after = len(later_rows)
        rows_identical = {_row_key(r) for r in earlier_rows} == {_row_key(r) for r in later_rows}

    return {
        "question": user_question,
        "earlier_timestamp": earlier.get("timestamp"),
        "later_timestamp": later.get("timestamp"),
        "earlier_final_answer": earlier.get("final_answer", ""),
        "later_final_answer": later.get("final_answer", ""),
        "final_answer_changed": earlier.get("final_answer") != later.get("final_answer"),
        "sql_changed": sql_changed,
        "columns_changed": columns_changed,
        "row_count_before": row_count_before,
        "row_count_after": row_count_after,
        "rows_identical": rows_identical,
        "earlier_rows": earlier_rows,
        "later_rows": later_rows,
    }


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:max_len] or "question"


def _status_line(changed, changed_word: str, unchanged_word: str, na_word: str) -> str:
    if changed is None:
        return f'<p class="na">{na_word}</p>'
    css_class = "changed" if changed else "unchanged"
    word = changed_word if changed else unchanged_word
    return f'<p class="{css_class}">{word}</p>'


def _rows_table_html(rows: list) -> str:
    if not rows:
        return "<p><em>No rows.</em></p>"
    columns = list(rows[0].keys())
    header = "".join(f"<th>{c}</th>" for c in columns)
    body = "".join(
        "<tr>" + "".join(f"<td>{row.get(c)}</td>" for c in columns) + "</tr>" for row in rows
    )
    return f"<table><tr>{header}</tr>{body}</table>"


def render_run_comparison_html(user_question: str) -> "str | None":
    """Build compare_runs(user_question) and render it as a plain HTML page
    under run_comparisons/. Returns None (not a broken file) if there's
    nothing to compare yet."""
    comparison = compare_runs(user_question)
    if comparison is None:
        return None

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Run Comparison</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; }}
table {{ border-collapse: collapse; margin: 0.5rem 0 1.5rem; }}
th, td {{ border: 1px solid #ccc; padding: 4px 8px; }}
.unchanged {{ color: #2a7a2a; }}
.changed {{ color: #c33; font-weight: bold; }}
.na {{ color: #888; font-style: italic; }}
</style>
</head>
<body>
<h1>Run Comparison</h1>
<p><b>Question:</b> {comparison['question']}</p>
{_status_line(comparison['sql_changed'], "SQL query changed between runs.", "SQL query did not change between runs.", "No SQL query in either run to compare.")}
{_status_line(None if comparison['rows_identical'] is None else not comparison['rows_identical'], "Result rows changed between runs.", "Result rows are identical between runs.", "No result rows in either run to compare.")}
{_status_line(comparison['final_answer_changed'], "Final answer changed.", "Final answer did not change.", "")}
<h2>Earlier run &mdash; {comparison['earlier_timestamp']}</h2>
<p>{comparison['earlier_final_answer']}</p>
{_rows_table_html(comparison['earlier_rows'])}
<h2>Later run &mdash; {comparison['later_timestamp']}</h2>
<p>{comparison['later_final_answer']}</p>
{_rows_table_html(comparison['later_rows'])}
</body>
</html>
"""

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{_slugify(user_question)}_{timestamp}.html"
    out_path.write_text(html)
    return str(out_path)
