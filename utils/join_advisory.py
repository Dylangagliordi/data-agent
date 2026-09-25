"""Join Advisory (Spec 9, Part 2): a proactive, whole-schema relationship map,
not a per-query warning.

Fan-out detection already exists in this project (_fanout_status,
agents/sql_analyst.py:_detect_fanout_warnings) but only fires REACTIVELY —
injected into add_context's context right before a query that happens to
touch a risky table runs. You only find out a join is risky after you've
already asked a question that needed it. This surfaces the whole picture
up front, before any question is asked: which tables actually connect, on
what column, and whether that join has fan-out (needs pre-aggregation).

Two kinds of relationship, always labeled separately so certainty is never
overstated:
- "declared": a real FOREIGN KEY constraint in information_schema — ground
  truth, not inferred.
- "inferred": a same-named column that is a likely primary key in one table
  (_fanout_status.is_likely_fk = False) and a likely foreign key in another
  (is_likely_fk = True) with no declared constraint between them — a real,
  cheap, deterministic signal built entirely from data this project already
  computed, but explicitly NOT the same certainty as a declared constraint.

CLI: `python main.py "joins"`.
"""

import html
from datetime import datetime, timezone
from pathlib import Path

from utils.load_data import get_admin_connection

OUTPUT_DIR = "join_advisory"


def _declared_foreign_keys(conn) -> list:
    """Every real FOREIGN KEY constraint in the public schema — ground truth,
    never inferred. Reads pg_catalog.pg_constraint directly (conkey/confkey,
    the constraint's own column-number arrays) rather than
    information_schema.table_constraints/key_column_usage/
    constraint_column_usage: that trio fans out into a full cross-join of a
    composite key's columns against itself (constraint_column_usage carries
    no ordinal position to pair them back up correctly), while
    unnest(conkey, confkey) pairs each local column with its real referenced
    column position-by-position, correct for both single- and multi-column
    keys. Fully parameterized — no identifier interpolation.

    Requires the ADMIN connection, not app_reader: PostgreSQL's
    information_schema constraint views (confirmed live) only show rows to a
    role with an ownership-level relationship to the table — plain SELECT
    privilege (all app_reader has) isn't enough, and the same restriction
    applies to pg_constraint's own row-level visibility. This never writes
    anything — same "admin connection used for a legitimate non-mutating
    read" pattern utils/load_data.py's own tooling already relies on — so it
    doesn't weaken the real security boundary (app_reader being the ONLY role
    the live, LLM-driven generate_sql path ever uses); a fixed, hand-written
    reporting tool like this one has none of that path's attack surface.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                c.conrelid::regclass::text AS table_name,
                a.attname AS column_name,
                c.confrelid::regclass::text AS referenced_table,
                af.attname AS referenced_column
            FROM pg_constraint c
            CROSS JOIN LATERAL unnest(c.conkey, c.confkey) AS cols(attnum, refattnum)
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = cols.attnum
            JOIN pg_attribute af ON af.attrelid = c.confrelid AND af.attnum = cols.refattnum
            WHERE c.contype = %s AND c.connamespace = %s::regnamespace
            ORDER BY table_name, column_name
            """,
            ("f", "public"),
        )
        return [
            {"table": r[0], "column": r[1], "referenced_table": r[2], "referenced_column": r[3]}
            for r in cur.fetchall()
        ]


def _fanout_status_rows(conn) -> list:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, is_likely_fk, has_fanout FROM _fanout_status"
        )
        return cur.fetchall()


def _inferred_relationships(fanout_rows: list, declared: list) -> list:
    """Real, deterministic name-matching over _fanout_status: a column name
    that is a likely PK in one table (is_likely_fk = False) and a likely FK
    in a DIFFERENT table (is_likely_fk = True) with no declared constraint
    already covering that exact (table, column, referenced_table) triple.
    Never a guess about meaning — purely "these two tables share a column
    name with matching key-shape signals already computed elsewhere.\""""
    declared_triples = {(d["table"], d["column"], d["referenced_table"]) for d in declared}

    by_column: dict = {}
    for table_name, column_name, is_likely_fk, has_fanout in fanout_rows:
        by_column.setdefault(column_name, []).append(
            {"table": table_name, "is_likely_fk": is_likely_fk, "has_fanout": has_fanout}
        )

    inferred = []
    for column_name, entries in by_column.items():
        pk_tables = [e["table"] for e in entries if not e["is_likely_fk"]]
        fk_entries = [e for e in entries if e["is_likely_fk"]]
        for fk_entry in fk_entries:
            for pk_table in pk_tables:
                if pk_table == fk_entry["table"]:
                    continue
                triple = (fk_entry["table"], column_name, pk_table)
                if triple in declared_triples:
                    continue
                inferred.append(
                    {
                        "table": fk_entry["table"],
                        "column": column_name,
                        "referenced_table": pk_table,
                        "referenced_column": column_name,
                        "has_fanout": fk_entry["has_fanout"],
                    }
                )
    return inferred


def _enrich_declared_with_fanout(declared: list, fanout_rows: list) -> list:
    """Attach the real _fanout_status.has_fanout value for each declared
    relationship's (table, column) when this project has actually checked it
    — a declared FK can still fan out on the "many" side. None (never a
    guessed default) when no _fanout_status row exists for that column yet."""
    fanout_by_table_column = {(t, c): hf for t, c, _fk, hf in fanout_rows}
    enriched = []
    for rel in declared:
        rel = dict(rel)
        rel["has_fanout"] = fanout_by_table_column.get((rel["table"], rel["column"]))
        enriched.append(rel)
    return enriched


def get_join_advisory() -> dict:
    """{"declared": [...], "inferred": [...]} — see module docstring for the
    real, verified difference in certainty between the two. Uses the admin
    connection (see _declared_foreign_keys' docstring for why) but only ever
    issues SELECT statements — never a write."""
    conn = get_admin_connection()
    try:
        declared = _declared_foreign_keys(conn)
        fanout_rows = _fanout_status_rows(conn)
        declared = _enrich_declared_with_fanout(declared, fanout_rows)
        inferred = _inferred_relationships(fanout_rows, declared)
        conn.rollback()  # read-only; release any lock rather than holding one
    finally:
        conn.close()
    return {"declared": declared, "inferred": inferred}


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _fanout_cell(has_fanout) -> str:
    if has_fanout is None:
        return "not checked"
    return "HAS FAN-OUT — aggregate before joining" if has_fanout else "no fan-out"


def _relationships_html(relationships: list) -> str:
    if not relationships:
        return "<p><em>None found.</em></p>"
    rows = "".join(
        f"<tr><td>{_esc(r['table'])}.{_esc(r['column'])}</td>"
        f"<td>{_esc(r['referenced_table'])}.{_esc(r['referenced_column'])}</td>"
        f"<td>{_fanout_cell(r['has_fanout'])}</td></tr>"
        for r in relationships
    )
    return (
        "<table><thead><tr><th>Column</th><th>References</th><th>Fan-out</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def render_join_advisory_html() -> str:
    advisory = get_join_advisory()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    full_html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Join Advisory</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
table {{ border-collapse: collapse; width: 100%; margin: 0.5rem 0 1.5rem; font-size: 0.92em; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
th {{ background: #f2f5f9; }}
.meta {{ color: #666; font-size: 0.88em; }}
.note {{ background: #fff8e1; border-left: 4px solid #f9a825; padding: 10px 14px; border-radius: 3px; margin: 1rem 0; font-size: 0.92em; }}
</style>
</head>
<body>
<h1>Join Advisory</h1>
<p class="meta">Generated {generated_at} &mdash; {len(advisory['declared'])} declared relationship(s), {len(advisory['inferred'])} inferred relationship(s)</p>
<h2>Declared foreign keys</h2>
<p class="meta">Real FOREIGN KEY constraints — ground truth, not inferred.</p>
{_relationships_html(advisory['declared'])}
<h2>Inferred relationships</h2>
<div class="note">These are name-matched from already-computed key-shape signals (_fanout_status), not
declared constraints — a real, cheap signal, but not the same certainty as the table above.</div>
{_relationships_html(advisory['inferred'])}
</body>
</html>
"""
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"join_advisory_{timestamp}.html"
    out_path.write_text(full_html, encoding="utf-8")
    return str(out_path)
