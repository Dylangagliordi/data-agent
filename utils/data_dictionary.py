"""
Data dictionary (Spec 2): a browsable "what does this column mean, and can I
trust it" view for one table, built entirely from metadata this project
already computes — information_schema, _data_quality_status, _fanout_status,
_derived_columns, _transformation_candidates. The only live query beyond
those existing tables is a single COUNT(*) for row count; nothing here
profiles or re-detects anything new.

Absence is never fabricated into a default: a column with no _fanout_status
row is reported as "no fan-out check recorded", not defaulted to "not a key";
a table with no _data_quality_status row is reported as "never processed by
this pipeline", not defaulted to "pass".

CLI trigger: `python main.py "dictionary: <table_name>"`.
See tests/test_data_dictionary.py.
"""

from datetime import datetime, timezone
from pathlib import Path

from utils.db import get_app_reader_connection
from utils.load_data import (
    get_derived_columns,
    read_data_quality_status,
    read_fanout_status,
    read_transformation_candidates,
)

OUTPUT_DIR = "data_dictionaries"


def generate_data_dictionary(table_name: str) -> dict:
    """Build a data dictionary dict for table_name, reading only via the
    read-only app_reader connection — real columns/types from
    information_schema, a plain row count, and whatever quality/fanout/
    derived/candidate metadata this project already has on file for it.
    """
    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = %s
                ORDER BY ordinal_position
                """,
                (table_name,),
            )
            column_rows = cur.fetchall()

            # table_name here is never string-built into the identifier from
            # user/network input at the call sites this module is used from
            # (a CLI arg the operator themselves types) — same discipline as
            # the identifier-building already documented in load_data.py.
            cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
            row_count = cur.fetchone()[0]
        conn.rollback()  # read-only; release any lock rather than holding one

        quality_status = read_data_quality_status(conn, table_name)
        fanout_by_column = read_fanout_status(conn, table_name)
        derived_by_column = get_derived_columns(conn, table_name)
        candidates = read_transformation_candidates(conn, table_name)
    finally:
        conn.close()

    high_cardinality_columns = set()
    for candidate in candidates:
        if candidate.kind == "categorical_consolidation":
            high_cardinality_columns.update(candidate.columns)

    columns = []
    for column_name, data_type in column_rows:
        fanout = fanout_by_column.get(column_name)
        if fanout is None:
            role = "regular"
            has_fanout = None
        else:
            role = "foreign key" if fanout["is_likely_fk"] else "primary key"
            has_fanout = fanout["has_fanout"]

        columns.append(
            {
                "name": column_name,
                "data_type": data_type,
                "role": role,
                "fanout_source": fanout["source"] if fanout else None,
                "has_fanout": has_fanout,
                "is_derived": column_name in derived_by_column,
                "derived_kind": derived_by_column.get(column_name),
                "high_cardinality_candidate": column_name in high_cardinality_columns,
            }
        )

    return {
        "table_name": table_name,
        "row_count": row_count,
        "quality_status": quality_status,
        "columns": columns,
    }


def _quality_status_html(quality_status: "dict | None") -> str:
    if quality_status is None:
        return "<p><em>Never processed by this project's cleaning/loading pipeline — no recorded status.</em></p>"
    return (
        f"<p><b>Data quality status:</b> {quality_status['status']} "
        f"({len(quality_status['issues_found'])} unresolved issue(s)) &mdash; "
        f"last loaded {quality_status['last_loaded_at']}, "
        f"{'was' if quality_status['was_cleaned'] else 'was not'} cleaned.</p>"
    )


def _column_notes(column: dict) -> str:
    notes = []
    if column["role"] == "regular":
        notes.append("no fan-out check recorded")
    else:
        fanout_note = "has fan-out (one-to-many)" if column["has_fanout"] else "no fan-out"
        notes.append(f"{column['role']} &mdash; {fanout_note}")
    if column["is_derived"]:
        notes.append(f"DERIVED via {column['derived_kind']} &mdash; not an observed source value")
    if column["high_cardinality_candidate"]:
        notes.append("high-cardinality categorical &mdash; a consolidation candidate exists")
    return "; ".join(notes)


def render_data_dictionary_html(table_name: str) -> str:
    """Build generate_data_dictionary(table_name) and render it as a plain HTML
    page under data_dictionaries/. Returns the written path."""
    data = generate_data_dictionary(table_name)

    rows_html = "".join(
        f"<tr><td>{col['name']}</td><td>{col['data_type']}</td><td>{_column_notes(col)}</td></tr>"
        for col in data["columns"]
    )

    html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Data Dictionary: {table_name}</title>
<style>
body {{ font-family: sans-serif; margin: 2rem; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
th, td {{ border: 1px solid #ccc; padding: 6px 10px; text-align: left; }}
th {{ background: #f2f2f2; }}
</style>
</head>
<body>
<h1>Data Dictionary: {table_name}</h1>
<p><b>Row count:</b> {data['row_count']}</p>
{_quality_status_html(data['quality_status'])}
<table>
<tr><th>Column</th><th>Type</th><th>Notes</th></tr>
{rows_html}
</table>
</body>
</html>
"""

    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{table_name}_{timestamp}.html"
    out_path.write_text(html)
    return str(out_path)
