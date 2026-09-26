"""Ingestion Source Registry viewer (Spec 10, Part 3): browses
_ingestion_sources, the durable record of every URL
agents/etl_analyst.py:extract_load/scrape_load have ever been asked to fetch
(utils/load_data.py:ensure_ingestion_sources_table/record_ingestion_attempt/
read_ingestion_sources).

Pure read — this module writes nothing; the registry itself is only ever
written from inside extract_load/scrape_load, as a side effect of a real
fetch attempt (successful or not).

CLI trigger: `python main.py "sources"`.
"""

import html
from datetime import datetime, timezone
from pathlib import Path

from utils.load_data import ensure_ingestion_sources_table, get_admin_connection, read_ingestion_sources

OUTPUT_DIR = "ingestion_sources"


def get_ingestion_sources() -> list:
    """Every tracked source, most recently fetched first. Uses the admin
    connection only because ensure_* needs write access to create the table
    on a fresh database the first time this is ever called — the read itself
    (read_ingestion_sources) is a plain SELECT, and app_reader already has
    SELECT granted on this table for any other reader that wants it."""
    conn = get_admin_connection()
    try:
        ensure_ingestion_sources_table(conn)
        sources = read_ingestion_sources(conn)
    finally:
        conn.close()
    return sources


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def render_ingestion_sources_html() -> str:
    sources = get_ingestion_sources()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not sources:
        rows_html = "<tr><td colspan='7'><em>No sources fetched yet.</em></td></tr>"
    else:
        row_parts = []
        for s in sources:
            status_cell = _esc(s["status"])
            if s["status"] == "error":
                status_cell += f" &mdash; {_esc(s['last_error'])}"
            row_parts.append(
                f"<tr><td>{_esc(s['url'])}</td><td>{_esc(s['source_kind'])}</td>"
                f"<td>{_esc(s['output_folder'])}</td><td>{s['fetch_count']}</td>"
                f"<td>{_esc(s['first_fetched_at'])}</td><td>{_esc(s['last_fetched_at'])}</td>"
                f"<td class='{'bad' if s['status'] == 'error' else 'ok'}'>{status_cell}</td></tr>"
            )
        rows_html = "".join(row_parts)

    full_html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Ingestion Sources</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.88em; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; word-break: break-all; }}
th {{ background: #f2f5f9; }}
.meta {{ color: #666; font-size: 0.88em; }}
.ok {{ color: #2a7a2a; }}
.bad {{ color: #c33; font-weight: 600; }}
</style>
</head>
<body>
<h1>Ingestion Sources</h1>
<p class="meta">Generated {generated_at} &mdash; {len(sources)} tracked source(s). This is a memory of what
extract_load/scrape_load have actually fetched before — not an allowlist; nothing here blocks a fetch.</p>
<table>
<thead><tr><th>URL</th><th>Kind</th><th>Output folder</th><th>Fetch count</th>
<th>First fetched</th><th>Last fetched</th><th>Last status</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
</body>
</html>
"""
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"ingestion_sources_{timestamp}.html"
    out_path.write_text(full_html, encoding="utf-8")
    return str(out_path)
