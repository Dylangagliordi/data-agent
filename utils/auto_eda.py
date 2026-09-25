"""Auto-EDA (Spec 9, Part 1): profile a table's real statistical shape with NO
specific question asked — the genuinely new capability this adds is that
today the SQL analyst can only answer a question already put into words; even
"what does this table look like" has to go through the full curate ->
add_context -> generate_sql -> is_safe -> execute -> answer pipeline, one
narrow question at a time. This computes real per-column statistics directly
via app_reader (read-only) SQL aggregates, no LLM, no specific question.

Complementary to utils/data_dictionary.py, not a duplicate: the data
dictionary describes SCHEMA-level metadata (type, key role, derived-or-not,
already-computed high-cardinality flags from _transformation_candidates)
already tracked elsewhere; this computes the actual statistical shape of the
data itself (distributions, null rates, numeric spread) that nothing else in
this project currently measures.

The "high cardinality" categorical flag reuses
utils.categorical_consolidation's real, already-established thresholds
(CATEGORICAL_CONSOLIDATION_MIN_DISTINCT/MIN_RATIO/MAX_RATIO) rather than
inventing a second, competing definition of "high cardinality" in this
project.

CLI: `python main.py "profile: <table_name>"`.
"""

import html
from datetime import datetime, timezone
from pathlib import Path

from utils.categorical_consolidation import (
    CATEGORICAL_CONSOLIDATION_MAX_RATIO,
    CATEGORICAL_CONSOLIDATION_MIN_DISTINCT,
    CATEGORICAL_CONSOLIDATION_MIN_RATIO,
)
from utils.db import get_app_reader_connection

OUTPUT_DIR = "auto_eda"

_NUMERIC_PG_TYPES = {
    "smallint", "integer", "bigint", "decimal", "numeric",
    "real", "double precision", "smallserial", "serial", "bigserial",
}


def _is_numeric_type(data_type: str) -> bool:
    return data_type.lower() in _NUMERIC_PG_TYPES


def _numeric_profile(cur, table_name: str, column_name: str, row_count: int) -> dict:
    # column_name/table_name are identifiers, never bindable as %s (Postgres
    # only parameterizes values) — both come straight from information_schema
    # here, never from network/user-supplied text beyond the CLI table-name
    # argument the operator themselves types, same discipline already
    # documented at every other identifier-interpolation site in this project
    # (agents/sql_analyst.py:add_context, utils/data_dictionary.py).
    cur.execute(
        f'SELECT COUNT(*) FILTER (WHERE "{column_name}" IS NULL), '
        f'COUNT(DISTINCT "{column_name}"), MIN("{column_name}"), MAX("{column_name}"), '
        f'AVG("{column_name}"::double precision), STDDEV("{column_name}"::double precision) '
        f'FROM "{table_name}"'
    )
    null_count, distinct_count, min_val, max_val, avg_val, stddev_val = cur.fetchone()
    return {
        "kind": "numeric",
        "null_count": null_count,
        "null_rate": (null_count / row_count) if row_count else 0.0,
        "distinct_count": distinct_count,
        "min": min_val,
        "max": max_val,
        "avg": float(avg_val) if avg_val is not None else None,
        "stddev": float(stddev_val) if stddev_val is not None else None,
    }


def _categorical_profile(cur, table_name: str, column_name: str, row_count: int) -> dict:
    cur.execute(
        f'SELECT COUNT(*) FILTER (WHERE "{column_name}" IS NULL), '
        f'COUNT(DISTINCT "{column_name}") FROM "{table_name}"'
    )
    null_count, distinct_count = cur.fetchone()

    cur.execute(
        f'SELECT "{column_name}", COUNT(*) AS n FROM "{table_name}" '
        f'WHERE "{column_name}" IS NOT NULL GROUP BY "{column_name}" '
        f'ORDER BY n DESC LIMIT %s',
        (5,),
    )
    top_values = [{"value": v, "count": c} for v, c in cur.fetchall()]

    ratio = (distinct_count / row_count) if row_count else 0.0
    is_high_cardinality = (
        distinct_count >= CATEGORICAL_CONSOLIDATION_MIN_DISTINCT
        and CATEGORICAL_CONSOLIDATION_MIN_RATIO <= ratio <= CATEGORICAL_CONSOLIDATION_MAX_RATIO
    )
    return {
        "kind": "categorical",
        "null_count": null_count,
        "null_rate": (null_count / row_count) if row_count else 0.0,
        "distinct_count": distinct_count,
        "top_values": top_values,
        "is_high_cardinality": is_high_cardinality,
    }


def profile_table(table_name: str) -> dict:
    """Real per-column statistics for table_name, computed live via app_reader
    (read-only) SQL aggregates — never estimated from a sample, never cached.
    Numeric-typed columns (per information_schema) get a numeric profile
    (null rate, distinct count, min/max/avg/stddev); everything else gets a
    categorical profile (null rate, distinct count, top-5 values by
    frequency, a high-cardinality flag using this project's one existing
    threshold definition)."""
    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                ("public", table_name),
            )
            column_rows = cur.fetchall()

            cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
            row_count = cur.fetchone()[0]

            columns = []
            for column_name, data_type in column_rows:
                if _is_numeric_type(data_type):
                    profile = _numeric_profile(cur, table_name, column_name, row_count)
                else:
                    profile = _categorical_profile(cur, table_name, column_name, row_count)
                profile["name"] = column_name
                profile["data_type"] = data_type
                columns.append(profile)
        conn.rollback()  # read-only; release any lock rather than holding one
    finally:
        conn.close()

    return {"table_name": table_name, "row_count": row_count, "columns": columns}


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _column_html(col: dict) -> str:
    if col["kind"] == "numeric":
        detail = (
            f"min {_esc(col['min'])}, max {_esc(col['max'])}, "
            f"avg {col['avg']:.2f}" if col["avg"] is not None else "no non-null values"
        )
        if col["avg"] is not None and col["stddev"] is not None:
            detail += f", stddev {col['stddev']:.2f}"
    else:
        top = ", ".join(f"{_esc(v['value'])} ({v['count']})" for v in col["top_values"])
        detail = f"top values: {top}" if top else "no non-null values"
        if col["is_high_cardinality"]:
            detail += " &mdash; high-cardinality categorical"

    return (
        f"<tr><td>{_esc(col['name'])}</td><td>{_esc(col['data_type'])}</td>"
        f"<td>{col['kind']}</td>"
        f"<td>{col['null_count']} ({col['null_rate']:.1%})</td>"
        f"<td>{col['distinct_count']}</td>"
        f"<td>{detail}</td></tr>"
    )


def render_auto_eda_html(table_name: str) -> str:
    profile = profile_table(table_name)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    rows_html = "".join(_column_html(col) for col in profile["columns"])

    full_html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Auto-EDA</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 1000px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; font-size: 0.92em; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; vertical-align: top; }}
th {{ background: #f2f5f9; }}
.meta {{ color: #666; font-size: 0.88em; }}
</style>
</head>
<body>
<h1>Auto-EDA: {_esc(table_name)}</h1>
<p class="meta">Generated {generated_at} &mdash; {profile['row_count']} row(s), {len(profile['columns'])} column(s)</p>
<table>
<thead><tr><th>Column</th><th>Type</th><th>Kind</th><th>Nulls</th><th>Distinct</th><th>Profile</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
</body>
</html>
"""
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"{table_name}_{timestamp}.html"
    out_path.write_text(full_html, encoding="utf-8")
    return str(out_path)
