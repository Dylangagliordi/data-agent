"""
DQ backlog view (Spec 3): every table with an outstanding fail- or warn-level
data-quality issue, ranked, in one place — instead of only ever being
surfaced reactively when a question happens to touch a flagged table.

Reads _data_quality_status only; computes nothing new, detects nothing new.
Ranking: fail-status tables before warn-status tables, then by issue count
descending within each tier.

CLI trigger: `python main.py "dq backlog"`.
See tests/test_dq_backlog.py.
"""

from datetime import datetime, timezone
from pathlib import Path

from utils.db import get_app_reader_connection

OUTPUT_DIR = "dq_backlog"


def get_dq_backlog() -> list:
    """Return every table currently at "fail" or "warn" status in
    _data_quality_status, ranked fail-first then by issue count descending.

    Returns [] — never an error — if _data_quality_status doesn't exist yet
    (a fresh database with nothing loaded) or if every loaded table is
    currently "pass".
    """
    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT to_regclass('_data_quality_status') IS NOT NULL")
            table_exists = cur.fetchone()[0]
            if not table_exists:
                return []

            cur.execute(
                """
                SELECT table_name, status, issues_found, was_cleaned, last_loaded_at
                FROM _data_quality_status
                WHERE status IN ('fail', 'warn')
                """
            )
            rows = cur.fetchall()
        conn.rollback()  # read-only; release any lock rather than holding one
    finally:
        conn.close()

    backlog = [
        {
            "table_name": table_name,
            "status": status,
            "issues": issues_found,
            "issue_count": len(issues_found),
            "was_cleaned": was_cleaned,
            "last_loaded_at": last_loaded_at.isoformat() if last_loaded_at is not None else None,
        }
        for table_name, status, issues_found, was_cleaned, last_loaded_at in rows
    ]
    backlog.sort(key=lambda entry: (0 if entry["status"] == "fail" else 1, -entry["issue_count"]))
    return backlog


def render_dq_backlog_html() -> str:
    """Render get_dq_backlog() as a plain HTML page under dq_backlog/.
    Returns the written path."""
    backlog = get_dq_backlog()

    if not backlog:
        body = "<p>No outstanding data-quality issues across any tracked table.</p>"
    else:
        sections = []
        for entry in backlog:
            issue_items = "".join(
                f"<li><b>{issue['severity']}</b>: {issue['issue']}</li>" for issue in entry["issues"]
            )
            sections.append(
                f"<h2>{entry['table_name']} &mdash; {entry['status'].upper()} "
                f"({entry['issue_count']} issue(s))</h2>"
                f"<p>Last loaded {entry['last_loaded_at']}, "
                f"{'was' if entry['was_cleaned'] else 'was not'} cleaned.</p>"
                f"<ul>{issue_items}</ul>"
            )
        body = "".join(sections)

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Data Quality Backlog</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; }}
h2 {{ border-bottom: 1px solid #ccc; padding-bottom: 4px; }}
</style>
</head>
<body>
<h1>Data Quality Backlog</h1>
{body}
</body>
</html>
"""

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"dq_backlog_{timestamp}.html"
    out_path.write_text(html)
    return str(out_path)
