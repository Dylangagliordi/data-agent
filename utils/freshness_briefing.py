"""
Freshness / drift briefing (Spec 5): a proactive, all-tables-at-once view of
whether each table's real raw source file has changed since it was last
processed — reading the same checksum this project already records
(_data_quality_status.source_checksum), just run across every tracked table
instead of only reactively, per-question, for one table at a time.

This is a detection/reporting signal only, exactly like check_source_freshness
itself — nothing here triggers a re-clean automatically.

Three honest outcomes per table, never fabricated:
- changed=True: the file's real current bytes differ from what was recorded.
- changed=False: they match.
- changed=None, source_file_found=False: the file can't be found at all
  (moved/deleted/renamed) — genuinely unknown, not defaulted to "unchanged".

CLI trigger: `python main.py "freshness"`.
See tests/test_freshness_briefing.py.
"""

from datetime import datetime, timezone
from pathlib import Path

from agents.sql_analyst import _find_source_csv
from utils.db import get_app_reader_connection
from utils.load_data import compute_file_checksum, sanitize_identifier

OUTPUT_DIR = "freshness_briefing"


def get_freshness_briefing() -> list:
    """One entry per table with a recorded source_folder in
    _data_quality_status, ranked changed-first, then "can't tell", then
    unchanged. A table with no recorded source_folder is never included —
    there's nothing to compare it against."""
    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, source_folder, source_checksum
                FROM _data_quality_status
                WHERE source_folder IS NOT NULL
                """
            )
            rows = cur.fetchall()
        conn.rollback()  # read-only; release any lock rather than holding one
    finally:
        conn.close()

    briefing = []
    for table_name, source_folder, stored_checksum in rows:
        csv_path = _find_source_csv(Path(source_folder), table_name, sanitize_identifier)
        if csv_path is None:
            briefing.append(
                {
                    "table_name": table_name,
                    "source_folder": source_folder,
                    "source_file_found": False,
                    "changed": None,
                    "stored_checksum": stored_checksum,
                    "current_checksum": None,
                }
            )
            continue

        current_checksum = compute_file_checksum(csv_path)
        # Same semantics as utils.load_data.check_source_freshness: "changed"
        # only when a prior checksum exists AND it differs from the file's
        # current bytes. Computed inline (not via check_source_freshness
        # itself) since we already have every stored checksum from the one
        # bulk query above, rather than re-querying per table.
        changed = stored_checksum is not None and stored_checksum != current_checksum
        briefing.append(
            {
                "table_name": table_name,
                "source_folder": source_folder,
                "source_file_found": True,
                "changed": changed,
                "stored_checksum": stored_checksum,
                "current_checksum": current_checksum,
            }
        )

    def _rank(entry):
        if entry["changed"]:
            return 0
        if not entry["source_file_found"]:
            return 1
        return 2

    briefing.sort(key=_rank)
    return briefing


def _status_label(entry: dict) -> str:
    if not entry["source_file_found"]:
        return '<span class="unknown">source file not found &mdash; cannot determine freshness</span>'
    if entry["changed"]:
        return '<span class="changed">source file has changed since last processed</span>'
    return '<span class="unchanged">up to date</span>'


def render_freshness_briefing_html() -> str:
    """Render get_freshness_briefing() as a plain HTML page under
    freshness_briefing/. Returns the written path."""
    briefing = get_freshness_briefing()

    if not briefing:
        body = "<p>No tables have a recorded source folder to check.</p>"
    else:
        rows_html = "".join(
            f"<tr><td>{entry['table_name']}</td><td>{entry['source_folder']}</td>"
            f"<td>{_status_label(entry)}</td></tr>"
            for entry in briefing
        )
        body = f"<table><tr><th>Table</th><th>Source folder</th><th>Status</th></tr>{rows_html}</table>"

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Freshness Briefing</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
th {{ background: #f2f2f2; }}
.changed {{ color: #c33; font-weight: bold; }}
.unchanged {{ color: #2a7a2a; }}
.unknown {{ color: #888; font-style: italic; }}
</style>
</head>
<body>
<h1>Freshness Briefing</h1>
{body}
</body>
</html>
"""

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"freshness_briefing_{timestamp}.html"
    out_path.write_text(html)
    return str(out_path)
