"""
SQL analyst sub-agent: LangGraph node definitions.
"""

import csv
import datetime as _dt
import decimal as _decimal
import json
import re
import statistics
from pathlib import Path
from typing import Literal

import sqlglot
from sqlglot import exp
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from pydantic import create_model

from models.schema import ChartTypeSchema, JudgeSchema, SQLAnalystState
from utils.db import get_app_reader_connection
from utils.llm_pick import pick_llm

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Result parsing ────────────────────────────────────────────────────────────


def _parse_sql_result(result_str: str) -> tuple[list, bool]:
    """Parse execute_sql's JSON result string into a list of row dicts.

    execute_sql serializes rows as {"columns": [...], "rows": [[...], ...], "truncated": bool}.
    Decimal values are stored as floats and datetime values as ISO strings at
    serialization time, so no regex reconstruction or eval is needed here.

    Returns (rows_as_list_of_dicts, was_truncated).
    Returns ([], False) for error strings (SQL_EXECUTION_ERROR: prefix) or any
    unparseable input — callers treat that as "no data".
    """
    if not result_str or result_str.startswith(_SQL_ERROR_PREFIX):
        return [], False
    try:
        data = json.loads(result_str)
        cols = data["columns"]
        rows = [dict(zip(cols, row)) for row in data["rows"]]
        return rows, bool(data.get("truncated", False))
    except Exception:
        return [], False

_ID_COLUMN_RE = re.compile(r"_id$")


def _is_id_like_column(column_name: str) -> bool:
    """True for column names that look like an entity identifier: exactly \"id\", \
or anything ending in \"_id\" (order_id, customer_id, product_id, ...). This is a naming \
convention, not a hardcoded list of table/column names, so it applies to any dataset that \
follows the common `<entity>_id` convention — which is how Olist (and most relational \
exports) name keys."""
    return column_name == "id" or bool(_ID_COLUMN_RE.search(column_name))


def _read_fanout_from_metadata(conn, table_names: list) -> tuple:
    """Read precomputed fan-out metadata from _fanout_status for the given tables.

    Returns (warnings, uncovered_tables) where:
    - warnings: list of warning strings for (is_likely_fk=True AND has_fanout=True) rows
    - uncovered_tables: table names with no rows in _fanout_status (need live fallback)

    If the _fanout_status table doesn't exist at all (e.g. fresh DB before any load),
    all table_names are returned as uncovered so the caller falls back to live checks.
    """
    if not table_names:
        return [], []
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public._fanout_status",))
        (table_exists,) = cur.fetchone()
        if not table_exists:
            return [], list(table_names)

        placeholders = ", ".join(["%s"] * len(table_names))
        cur.execute(
            f"SELECT DISTINCT table_name FROM _fanout_status WHERE table_name IN ({placeholders})",
            tuple(table_names),
        )
        covered = {row[0] for row in cur.fetchall()}
        uncovered = [t for t in table_names if t not in covered]

        if not covered:
            return [], uncovered

        covered_ph = ", ".join(["%s"] * len(covered))
        cur.execute(
            f"""
            SELECT table_name, column_name, source
            FROM _fanout_status
            WHERE table_name IN ({covered_ph})
              AND is_likely_fk = TRUE
              AND has_fanout   = TRUE
            """,
            tuple(covered),
        )
        fanout_rows = cur.fetchall()

    _source_labels = {
        "declared_fk": "declared foreign key",
        "cardinality_heuristic": "inferred from data distribution",
    }
    warnings = [
        f"WARNING: {tbl} has multiple rows per {col} "
        f"(fan-out risk — {_source_labels.get(src, 'inferred from data distribution')}; "
        "aggregate before joining)."
        for tbl, col, src in fanout_rows
    ]
    return warnings, uncovered


def _detect_fanout_warnings(conn, tables: dict, warn_only_for: "set | None" = None) -> list:
    """Live, deterministic fan-out check against whatever tables/columns actually exist.

    For every id-like column in every table, ask Postgres directly (via a real
    COUNT(*)/COUNT(DISTINCT ...) query) whether that column holds duplicate values within
    that table. No table or column names are hardcoded anywhere in this function — every
    table, column, and query target is re-derived from `tables` (itself built from
    information_schema at call time), so this automatically re-runs correctly against
    whatever new dataset gets loaded via utils/load_data.py in the future, with no manual
    updates needed here.

    Two-pass approach:
      1. For every id-like column, check whether it's unique within its own table. An
         id-like column that IS unique in its own table is a plausible primary key for
         that table — record its name so pass 2 can recognize other tables' columns
         sharing that name as candidate foreign keys ("matches another table's primary
         key name").
      2. Any column that looks like a foreign key — it ends in "_id" (or is named "id")
         and isn't that table's own primary key, OR its name matches a primary-key name
         found on another table — gets a real duplicate check. Duplicates found mean this
         table has more than one row per that key: a genuine one-to-many/fan-out
         relationship relative to whatever it references, which is exactly the risk
         generate_sql's prompt warns about (payments per order, reviews per product, etc.).
    """
    uniqueness_cache: dict = {}

    def total_and_distinct(table: str, column: str):
        key = (table, column)
        if key in uniqueness_cache:
            return uniqueness_cache[key]
        with conn.cursor() as cur:
            # table/column here come straight out of information_schema, not user
            # input — same rationale as the sample-rows query above: identifiers can't
            # be bound as %s placeholders, so it's safe (not user-controlled) to
            # interpolate them into identifier position, while the LIMIT-equivalent
            # value (none needed here) would still go through a placeholder.
            cur.execute(f'SELECT COUNT(*), COUNT(DISTINCT "{column}") FROM "{table}"')
            result = cur.fetchone()
        uniqueness_cache[key] = result
        return result

    own_pk_columns: dict = {}
    pk_names: set = set()
    for table, columns in tables.items():
        own_pk_columns[table] = set()
        for column, _dtype in columns:
            if not _is_id_like_column(column):
                continue
            total, distinct = total_and_distinct(table, column)
            if total == distinct:
                own_pk_columns[table].add(column)
                pk_names.add(column)

    warn_tables = tables if warn_only_for is None else {
        t: cols for t, cols in tables.items() if t in warn_only_for
    }

    warnings = []
    for table, columns in warn_tables.items():
        for column, _dtype in columns:
            is_own_pk = column in own_pk_columns[table]
            looks_like_fk = (not is_own_pk) and (
                _is_id_like_column(column) or column in pk_names
            )
            if not looks_like_fk:
                continue
            total, distinct = total_and_distinct(table, column)
            if total > distinct:
                warnings.append(
                    f"WARNING: {table} has multiple rows per {column} "
                    "(fan-out risk — inferred from data distribution; aggregate before joining)."
                )
    return warnings


def _fetch_data_quality_status(conn, table_names: list) -> dict:
    """Look up each table's row (if any) in _data_quality_status, keyed by table name.

    Tables the loader has never seen (dropped in mid-development, or loaded before
    this system existed) simply have no row — that absence is itself meaningful (see
    add_context below), not an error, so this returns whatever subset of table_names
    actually has a row rather than raising or padding in fake entries.

    Defensive: _data_quality_status itself might not exist yet (e.g. a fresh DB that
    hasn't had utils/load_data.py run against it since this feature shipped) — in that
    case every table is treated as having no status row, same as a genuinely missing
    row, rather than crashing add_context.

    Returns tuples of (status, issues_found, source_folder). source_folder is None
    when the column doesn't exist yet (pre-migration schema) or when the row pre-dates
    the source_folder feature — both mean "can't auto-redirect for this table".
    """
    if not table_names:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT to_regclass(%s) IS NOT NULL", ("public._data_quality_status",)
        )
        (table_exists,) = cur.fetchone()
        if not table_exists:
            return {}

        # source_folder was added after the initial schema; check before querying so
        # that an older schema that hasn't run load_data.py yet still works cleanly.
        cur.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name = '_data_quality_status'
              AND column_name = 'source_folder'
            """
        )
        (has_source_folder,) = cur.fetchone()

        placeholders = ", ".join(["%s"] * len(table_names))
        if has_source_folder:
            cur.execute(
                f"""
                SELECT table_name, status, issues_found, source_folder
                FROM _data_quality_status
                WHERE table_name IN ({placeholders})
                """,
                tuple(table_names),
            )
            rows = cur.fetchall()
            return {
                table_name: (status, issues_found, source_folder)
                for table_name, status, issues_found, source_folder in rows
            }
        else:
            cur.execute(
                f"""
                SELECT table_name, status, issues_found
                FROM _data_quality_status
                WHERE table_name IN ({placeholders})
                """,
                tuple(table_names),
            )
            rows = cur.fetchall()
            return {
                table_name: (status, issues_found, None)
                for table_name, status, issues_found in rows
            }


def _summarize_fail_issues(issues_found) -> str:
    """One short human-readable summary of the fail-level issues in a status row's
    issues_found jsonb payload, for the WARNING line injected into generate_sql's
    context. issues_found is a list of {"issue": ..., "severity": ...} dicts (see
    utils/load_data.py's write_data_quality_status) — this picks out only the
    fail-severity ones, since that's the only case this helper is ever called for.
    """
    if not issues_found:
        return "(no details recorded)"
    fail_texts = [
        entry.get("issue", "") for entry in issues_found if entry.get("severity") == "fail"
    ]
    return "; ".join(fail_texts) if fail_texts else "(no details recorded)"


def _extract_text(content) -> str:
    """Extract plain text from a chat model response's .content.

    Most responses are a plain str, but some providers (confirmed live with
    claude-sonnet-5, which can engage extended thinking even without an
    explicit thinking config) return a list of content blocks instead, e.g.
    [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "..."}].
    Calling .strip() directly on that list crashes with AttributeError. This
    normalizes both shapes down to the actual answer text.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


CURATE_QUESTION_SYSTEM_PROMPT = """You clean up the wording of a raw user question about a \
database. Fix grammar, spelling, and phrasing only. Do NOT change what the question is \
actually asking, do NOT add new constraints, and do NOT answer it. Output only the cleaned-up \
question text, nothing else."""


def curate_question(state: SQLAnalystState) -> dict:
    """Node 1: clean up the raw question's wording only (no LLM reasoning about intent).

    Appends the curated question to messages as a HumanMessage.
    """
    llm = pick_llm("cheap")
    response = llm.invoke(
        [
            ("system", CURATE_QUESTION_SYSTEM_PROMPT),
            ("human", state.user_question),
        ]
    )
    curated = _extract_text(response.content).strip()

    return {
        "curated_question": curated,
        "messages": [HumanMessage(content=curated)],
    }


def add_context(state: SQLAnalystState) -> dict:
    """Node 2 (no LLM): query information_schema live for table/column/type info
    plus 5 sample rows per table currently in the database, and build one context
    string. Table names are never hardcoded — this is fully driven by whatever
    tables actually exist in the public schema at call time.

    Also runs a deterministic, dataset-agnostic fan-out check (see
    _detect_fanout_warnings) and prepends any resulting WARNING lines to the context,
    so generate_sql is told explicitly, every call, which tables in THIS dataset have
    more than one row per some referenced key — rather than relying on the prompt's
    general rule alone to be remembered.

    Also queries the persistent _data_quality_status table (written by
    utils/load_data.py) for every table currently in the database. Two cases are
    surfaced into generate_sql's context, prepended as WARNING lines exactly like the
    fan-out warnings above:
    - a table with NO status row at all (never went through the load_data.py path
      this system tracks) gets "WARNING: <table> has no recorded data-quality check."
    - a table whose latest status is "fail" (an unresolved critical issue survived
      cleaning) gets "WARNING: <table> has an unresolved critical data-quality issue:
      <summary>."
    "warn"-status tables are deliberately NOT injected here — real but not serious
    enough to affect query generation (see the module docstring / project spec). The
    full set of both warning kinds (never warn-level) is also returned as
    data_quality_warnings on state, for represent_final_answer to filter down to just
    the tables the actually-generated query touches.
    """
    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s
                  AND table_name NOT IN (%s, %s, %s, %s, %s, %s, %s, %s)
                ORDER BY table_name, ordinal_position
                """,
                (
                    "public", "_data_quality_status", "_fanout_status",
                    "_transformation_candidates", "_transformation_decisions",
                    "_derived_columns", "_cleaning_recipes", "_saved_metrics",
                    "_ingestion_sources",
                ),
            )
            rows = cur.fetchall()

        # Group columns by table, preserving order
        tables: dict[str, list[tuple[str, str]]] = {}
        for table_name, column_name, data_type in rows:
            tables.setdefault(table_name, []).append((column_name, data_type))

        fanout_warnings, uncovered = _read_fanout_from_metadata(conn, list(tables.keys()))
        if uncovered:
            import sys as _sys
            print(
                f"[fan-out] no _fanout_status entry for {uncovered}; falling back to live check",
                file=_sys.stderr,
            )
            live_warnings = _detect_fanout_warnings(conn, tables, warn_only_for=set(uncovered))
            fanout_warnings = fanout_warnings + live_warnings

        status_by_table = _fetch_data_quality_status(conn, list(tables.keys()))
        data_quality_warnings = []
        tables_to_clean = []
        already_attempted = set(state.cleaning_attempted_tables)
        for table_name in tables:
            entry = status_by_table.get(table_name)
            if entry is None:
                data_quality_warnings.append(
                    {
                        "table": table_name,
                        "warning": f"WARNING: {table_name} has no recorded data-quality check.",
                    }
                )
                continue
            status, issues_found, source_folder = entry
            eligible_for_reclean = source_folder is not None and table_name not in already_attempted

            # Checksum freshness gate (architecture review point #22): a "pass"/"warn"
            # status row is otherwise treated as permanently authoritative — nothing
            # ever re-verifies it against the live source file. If the raw source has
            # genuinely changed since this table was last processed, that stale status
            # must not just be silently reused: force this table through clean_and_reload
            # exactly like a "fail" table would be, regardless of its recorded status.
            source_changed = False
            if status != "fail" and eligible_for_reclean:
                from utils.load_data import check_source_freshness, sanitize_identifier

                target_csv = _find_source_csv(Path(source_folder), table_name, sanitize_identifier)
                if target_csv is not None:
                    source_changed, _current_checksum = check_source_freshness(
                        conn, table_name, target_csv
                    )

            if status == "fail":
                summary = _summarize_fail_issues(issues_found)
                data_quality_warnings.append(
                    {
                        "table": table_name,
                        "warning": (
                            f"WARNING: {table_name} has an unresolved critical "
                            f"data-quality issue: {summary}."
                        ),
                    }
                )
                # Eligible for auto-clean if source_folder is known and this table
                # hasn't already been attempted this question (stop-once rule).
                if eligible_for_reclean:
                    tables_to_clean.append({"table": table_name, "source_folder": source_folder})
            elif source_changed:
                data_quality_warnings.append(
                    {
                        "table": table_name,
                        "warning": (
                            f"WARNING: {table_name}'s source file has changed since it was "
                            f"last processed — source changed, forcing fresh clean."
                        ),
                    }
                )
                tables_to_clean.append({"table": table_name, "source_folder": source_folder})
            # status == "warn" or "pass" with an unchanged source: nothing injected.

        sections = []
        with conn.cursor() as cur:
            for table_name, columns in tables.items():
                # Spec 1, Part 7: any column previously added by a Transformation
                # Options fix (feature derivation, range decomposition, label
                # simplification) is marked "derived, not source" here, straight in
                # the schema context generate_sql reads — so a derived value (e.g. a
                # computed company_age) is never presented, generated against, or
                # disclosed as an originally-observed fact.
                from utils.load_data import get_derived_columns

                derived_cols = get_derived_columns(conn, table_name)
                col_lines = "\n".join(
                    f"  - {c} ({t})"
                    + (f" [DERIVED via {derived_cols[c]} — not an observed source value]" if c in derived_cols else "")
                    for c, t in columns
                )

                # Table identifiers can't be bound as %s placeholders (Postgres only
                # parameterizes values, not identifiers) but table_name here comes
                # straight out of information_schema, not user input, so it's safe
                # to interpolate into the identifier position.
                cur.execute(f'SELECT * FROM "{table_name}" LIMIT %s', (5,))
                sample_rows = cur.fetchall()
                sample_col_names = [desc[0] for desc in cur.description]

                sample_lines = "\n".join(str(dict(zip(sample_col_names, r))) for r in sample_rows)

                sections.append(
                    f"Table: {table_name}\nColumns:\n{col_lines}\nSample rows (up to 5):\n{sample_lines}"
                )

        context = "\n\n".join(sections)
        warning_lines = fanout_warnings + [w["warning"] for w in data_quality_warnings]
        if warning_lines:
            context = "\n".join(warning_lines) + "\n\n" + context

        # Spec 8: Semantic Layer. Purely additive context — never forces
        # generate_sql to use a saved metric, just tells it one exists so an
        # already-agreed-upon definition (e.g. how "active customer" is
        # computed) doesn't get silently reinvented, differently, every time
        # someone asks about it. read_saved_metrics tolerates the table not
        # existing yet (returns []) since app_reader can never create it.
        from utils.load_data import read_saved_metrics

        saved_metrics = read_saved_metrics(conn)
        if saved_metrics:
            metric_lines = "\n".join(
                f"  - {m['metric_name']}: {m['sql_fragment']}"
                + (f"  ({m['description']})" if m["description"] else "")
                for m in saved_metrics
            )
            context += (
                "\n\nKnown canonical metric definitions (previously agreed upon — reuse "
                "one of these exactly when the question asks for what it defines, instead "
                f"of deriving it a new way):\n{metric_lines}"
            )
    finally:
        conn.close()

    data_quality_action = "needs_cleaning" if tables_to_clean else "proceed"
    return {
        "prompt_query_context": context,
        "data_quality_warnings": data_quality_warnings,
        "data_quality_action": data_quality_action,
        "tables_to_clean": tables_to_clean,
    }


# ── Export-target detection ───────────────────────────────────────────────────

# Explicit Tableau mention patterns (all lower-cased for comparison).
# We match the bare word "tableau" case-insensitively — it is specific enough
# that a false positive is essentially impossible in a data question, and we
# deliberately do NOT infer the target from any indirect cues.
_TABLEAU_RE = re.compile(r"\btableau\b", re.IGNORECASE)


def _detect_export_target(curated_question: str) -> str:
    """Return "tableau" when the question explicitly names Tableau; "csv" otherwise.

    Detection is purely lexical — we never guess or infer tool preference from
    phrasing that does not literally say "Tableau".  Power BI, Looker Studio,
    and Metabase all consume plain CSV without issue, so they have no dedicated
    export path.  Tableau is the single exception because it has a first-party
    binary extract format (.hyper) that can be embedded directly in Tableau
    workbooks / data sources and loads substantially faster than CSV at scale.
    """
    if _TABLEAU_RE.search(curated_question):
        return "tableau"
    return "csv"


# ── Hyper file export ─────────────────────────────────────────────────────────

def _python_to_hyper_type(value):
    """Map a live Python value to the corresponding Tableau hyper SqlType.

    Inspect the first non-None value encountered for a column. Falls back to
    SqlType.text() for unknown types or all-None columns.
    """
    from tableauhyperapi import SqlType
    if isinstance(value, bool):
        return SqlType.bool()
    if isinstance(value, int):
        return SqlType.big_int()
    if isinstance(value, float):
        return SqlType.double()
    if isinstance(value, _dt.datetime):
        return SqlType.timestamp()
    if isinstance(value, _dt.date):
        return SqlType.date()
    return SqlType.text()


def _write_hyper_file(data: list, csv_path: Path) -> Path:
    """Write a Tableau .hyper extract file from parsed query-result data.

    Saved alongside the CSV in the same directory with the same stem but a
    .hyper extension.  Column types are inferred from the first non-None value
    in each column; all-None columns fall back to text.

    Why only Tableau and not Power BI / Looker Studio / Metabase?
    Power BI, Looker Studio, and Metabase all accept plain CSV natively and
    import it without any type guessing or performance penalty at the sizes
    this system produces.  Tableau is the sole exception: its .hyper format is
    a proprietary binary extract (Hyper database) that embeds directly into
    Tableau workbooks and data sources, preserves column types without
    heuristic inference, and loads significantly faster than CSV for large
    datasets.  There is no equivalent first-party binary format worth
    pre-building for the other tools.
    """
    from tableauhyperapi import (
        Connection,
        CreateMode,
        HyperProcess,
        Inserter,
        SqlType,
        TableDefinition,
        TableName,
        Telemetry,
    )

    hyper_path = csv_path.with_suffix(".hyper")

    if not data or not isinstance(data[0], dict):
        # Empty result: write a valid but empty .hyper so downstream callers
        # always receive a file at the promised path.
        with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
            with Connection(hyper.endpoint, str(hyper_path), CreateMode.CREATE_AND_REPLACE):
                pass
        return hyper_path

    raw_cols = list(data[0].keys())
    human_cols = [_humanize_column(c) for c in raw_cols]

    # Infer each column's Hyper type from the first non-None value in that column.
    col_types = []
    for rc in raw_cols:
        sample = next((row[rc] for row in data if row.get(rc) is not None), None)
        col_types.append(_python_to_hyper_type(sample))

    table_def = TableDefinition(
        TableName("Extract", "Extract"),
        [TableDefinition.Column(hc, ct) for hc, ct in zip(human_cols, col_types)],
    )

    with HyperProcess(telemetry=Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU) as hyper:
        with Connection(hyper.endpoint, str(hyper_path), CreateMode.CREATE_AND_REPLACE) as conn:
            conn.catalog.create_schema_if_not_exists("Extract")
            conn.catalog.create_table_if_not_exists(table_def)
            with Inserter(conn, table_def) as ins:
                for row in data:
                    ins.add_row([row[rc] for rc in raw_cols])
                ins.execute()

    return hyper_path


DETERMINE_CHART_TYPE_SYSTEM_PROMPT = """You are a data visualization expert. Given a user question, \
determine the most appropriate chart type using this STRICT, ORDERED priority list. Evaluate the \
rules in order and stop at the FIRST one that matches — never skip ahead to a later rule just \
because it also seems plausible, and never fall back to "genuinely uncertain, pick your best \
guess": one of these rules always applies, and rule 7 is the guaranteed catch-all.

1. Explicit chart type named in the question — e.g. "bar chart", "line graph", "pie chart", \
"scatter plot", "histogram", "box plot", "donut chart", "stacked bar", "treemap". Use it exactly \
as chart_type, verbatim. Set chart_type_source to "explicit" and chart_type_reasoning to an empty \
string — no justification is needed for something the user already specified.

If no chart type is named, evaluate rules 2-7 below IN ORDER and use the first one whose \
condition is met. For every one of these, set chart_type_source to "reasoned", and \
chart_type_reasoning MUST explicitly name which numbered rule fired (e.g. "Rule 4: the question \
asks for a breakdown of a small, explicitly named set of categories, so pie chart applies.") — \
never a generic justification like "best fit" or "seems appropriate".

2. Explicit time/trend language ("over time", "by month/year/quarter", "trend", "since X") \
→ line chart.
3. Explicit distribution language ("distribution of", "spread of", "how X varies") with no \
grouping implied → histogram. The same distribution language WITH an explicit grouping \
("...across regions/segments") → box plot.
4. Explicit part-to-whole language ("share of", "% of total", "breakdown of") → pie chart, but \
ONLY if the question implies a small, fixed set of categories — either named explicitly in the \
question, or phrased as "top N" with N <= 5. Otherwise (an unbounded or large category set) \
→ bar chart.
5. Two explicit numeric measures being related to each other ("relationship between X and Y", \
"does X correlate with Y") → scatter plot.
6. A second categorical dimension nested inside the first ("X broken down by Y within each Z") \
→ stacked bar chart — or treemap specifically when the question uses explicitly hierarchical \
language ("hierarchy", "nested", "drill down").
7. Default (the catch-all — use this whenever none of rules 2-6 matched): a plain category \
comparison, "top N", or "which X has the highest Y" → bar chart."""


def determine_chart_type(state: SQLAnalystState) -> dict:
    """Node: given the curated question, choose the chart type and export target.

    Runs only when wants_visualization=True — the conditional edge from
    add_context routes here only on that path; normal sql_analyst questions
    never reach this node at all.

    Chart type uses ChartTypeSchema via with_structured_output so chart_type_source
    is always one of the two valid literals and chart_type is never empty.

    Export target is detected deterministically (no LLM): "tableau" iff the
    curated question explicitly names Tableau, "csv" otherwise.
    """
    llm = pick_llm("cheap").with_structured_output(ChartTypeSchema)
    result: ChartTypeSchema = llm.invoke(
        [
            ("system", DETERMINE_CHART_TYPE_SYSTEM_PROMPT),
            ("human", state.curated_question),
        ]
    )
    return {
        "chart_type": result.chart_type,
        "chart_type_source": result.chart_type_source,
        "chart_type_reasoning": result.chart_type_reasoning,
        "export_target": _detect_export_target(state.curated_question),
    }


# ── Chart column resolution (by meaning, not SQL SELECT-list position) ─────────
#
# Root cause of a real bug: a query returning `industry, avg_salary, avg_rating,
# job_count` for a question about employee SATISFACTION plotted avg_salary
# (position 1) instead of avg_rating, purely because salary happened to be
# selected first in the SQL. The chart silently answered the wrong question
# while the LLM-written report prose (generated separately, from the same
# result data) correctly discussed rating — text and chart disagreed with no
# error or warning anywhere. resolve_chart_columns fixes this by asking, after
# the real result exists, which actual column the question meant — constrained
# so the model can only ever choose a column name that is really present.

RESOLVE_CHART_COLUMNS_SYSTEM_PROMPT = """You are given a user's question, the SQL query that was \
actually executed to answer it, and the real columns present in the query's result (each labeled \
numeric or non-numeric based on the actual data returned).

Pick exactly one category_column — what each row/bar/point represents, usually the non-numeric \
grouping column — and exactly one value_column — the specific metric the QUESTION is actually \
asking about, which is not always the first numeric column in the result or the SQL SELECT list. \
Read the question carefully: when the result has more than one numeric column, you must pick the \
one the question is actually about (e.g. a question about satisfaction/rating should pick the \
rating column even if a salary column appears earlier in the result).

Only pick secondary_column — a sub-category / nested dimension — when the chart type is \
"stacked bar" or "treemap"; for every other chart type, secondary_column MUST be an empty string.

You may only choose from the exact column names given to you — never invent, abbreviate, or \
guess at a column name that isn't in the list."""


def _build_chart_column_schema(columns: list):
    """Build a Pydantic model, per-call, whose fields are typed as Literal over the
    ACTUAL result column names present in THIS query's result — so the LLM is
    structurally unable to invent a column name that doesn't really exist.
    """
    col_literal = Literal[tuple(columns)]
    secondary_literal = Literal[tuple(columns) + ("",)]
    return create_model(
        "ChartColumnSchema",
        category_column=(col_literal, ...),
        value_column=(col_literal, ...),
        secondary_column=(secondary_literal, ...),
    )


def _fallback_chart_columns(cols: list, classification: dict) -> tuple:
    """Deterministic fallback used when the LLM pick fails or returns something
    invalid: category_column -> first non-numeric column, else cols[0];
    value_column -> first numeric column that isn't the category column, else
    cols[1] (or cols[0] if there's genuinely only one column)."""
    non_numeric = [c for c in cols if classification.get(c) != "numeric"]
    category = non_numeric[0] if non_numeric else cols[0]
    numeric_not_category = [
        c for c in cols if classification.get(c) == "numeric" and c != category
    ]
    if numeric_not_category:
        value = numeric_not_category[0]
    elif len(cols) > 1:
        value = cols[1]
    else:
        value = cols[0]
    return category, value


def resolve_chart_columns(state: SQLAnalystState) -> dict:
    """Node: after execute_sql succeeds on the visualization path, resolve which
    REAL result columns actually mean "category" / "value" / "secondary" for
    charting purposes — by meaning, not by SQL SELECT-list position.

    Only runs when wants_visualization is True (wired via route_after_execute_sql).
    If the result is empty or was truncated, resolution is skipped entirely and
    every chart_* field is left blank — build_visualization's existing truncation
    handling already covers that case, and there is nothing meaningful to resolve
    against an empty/partial result.
    """
    result_data, was_truncated = _parse_sql_result(state.sql_query_execution_result)
    if not result_data or was_truncated or not isinstance(result_data[0], dict):
        return {}

    cols = list(result_data[0].keys())
    first_row = result_data[0]
    classification = {
        c: ("numeric" if _to_float(first_row.get(c)) is not None else "non-numeric")
        for c in cols
    }

    try:
        schema_cls = _build_chart_column_schema(cols)
        llm = pick_llm("cheap").with_structured_output(schema_cls)
        classification_lines = "\n".join(
            f"  - {c}: {classification[c]}" for c in cols
        )
        human_content = (
            f"Question: {state.curated_question}\n\n"
            f"SQL executed:\n{state.generated_sql_query}\n\n"
            f"Chart type: {state.chart_type}\n\n"
            f"Real result columns:\n{classification_lines}"
        )
        result = llm.invoke(
            [
                ("system", RESOLVE_CHART_COLUMNS_SYSTEM_PROMPT),
                ("human", human_content),
            ]
        )
        category_col = result.category_column
        value_col = result.value_column
        secondary_col = result.secondary_column

        if category_col not in cols or value_col not in cols:
            raise ValueError("resolve_chart_columns: LLM returned a non-real column name")
        if secondary_col and secondary_col not in cols:
            raise ValueError("resolve_chart_columns: LLM returned a non-real secondary column")

        return {
            "chart_category_column": category_col,
            "chart_value_column": value_col,
            "chart_secondary_column": secondary_col,
            "chart_column_resolution_note": "",
        }
    except Exception:
        category_col, value_col = _fallback_chart_columns(cols, classification)
        return {
            "chart_category_column": category_col,
            "chart_value_column": value_col,
            "chart_secondary_column": "",
            "chart_column_resolution_note": (
                "Could not confidently identify which column the question meant to "
                "chart — used the first available metric column instead."
            ),
        }


GENERATE_SQL_SYSTEM_PROMPT = """You are a SQL analyst. Given a question and a description of \
the available tables (columns, types, and sample rows), write exactly ONE SQL query that \
answers the question against a PostgreSQL database.

Output ONLY the raw SQL query. No explanation, no commentary, no markdown code fences, no \
backticks — just the SQL statement itself.

Rules:
- If the question asks for a write/action (update, delete, insert, tier assignment, etc.) \
that this read-only query cannot actually perform, do NOT fake it by adding a literal/constant \
column (e.g. `'Gold' AS loyalty_tier`) that pretends the action happened. Write a plain SELECT \
that answers whatever part of the question is genuinely a read (e.g. "which customers spent \
the most"), and leave it at that — do not invent columns representing an action never executed.
- Do not return unbounded full-table results when the question implies a small, specific \
answer (e.g. the top customer(s), which category, how many) — use LIMIT, aggregation, \
or WHERE clauses so the result set is reasonably sized. Only omit a LIMIT if the question \
genuinely calls for every matching row.
- Minimum sample size for per-category ranking (FIXED PROJECT CONVENTION — do not pick your \
own number): whenever a question asks you to rank, compare, or identify the best/top/ \
highest category by an averaged or rate-based metric (e.g. which industry has the best \
X, which state has the highest average Y), you MUST add HAVING COUNT(*) >= 5 (using \
whatever the grouped row-count actually represents — job postings, orders, customers, etc.) \
unless the question itself states a different minimum explicitly. This value is fixed at 5 \
project-wide specifically so the same question produces the same included/excluded categories \
every time it's asked — it is NOT a judgment call to make fresh per query. A category with \
only 1-4 underlying rows is one anecdote, not a reliable average, and letting it in or out \
inconsistently is exactly the kind of silent variance this rule exists to prevent.
- Combined-metric ranking transparency: when a question asks to optimize for more than one \
metric at once (e.g. maximize both salary and job satisfaction), you must use a real, \
explicit ORDER BY over both metrics (e.g. ORDER BY primary_metric DESC, secondary_metric \
DESC) — never silently pick only one metric to sort by while mentioning the other only in \
prose. Put what you consider the primary metric first in the ORDER BY; this ordering is \
extracted mechanically from your SQL afterward and disclosed to the user automatically, so it \
must genuinely reflect the ranking logic you used, not just look plausible.
- Fan-out / grain check: before writing an aggregation (SUM, AVG, COUNT, etc.) that spans \
more than one table, consider whether any joined table could have more than one row per the \
unit you are measuring (e.g. more than one row per order, per product, per customer). Joining \
straight into such a table and aggregating over the joined result silently computes the wrong \
granularity — it double-counts or averages over the wrong thing. If a joined table can have \
multiple rows per the relevant key, first aggregate that table down to exactly one row per \
that key (a subquery or CTE with GROUP BY), and only then join it to the rest of the query or \
aggregate further. The database context below may include explicit "WARNING: ... fan-out \
risk" notes identifying which tables actually have this issue for the currently loaded data — \
treat those as confirmed, but apply this same reasoning even for tables not called out, since \
the context only flags what could be checked mechanically.
- Binning / bucketing: when a question implies grouping a continuous value into ranges \
(age groups, price brackets, order-size tiers, time-of-day slots, score bands, etc.), \
create the buckets directly in the query with a CASE WHEN expression — do NOT return raw, \
ungrouped values and let the caller bin them. GROUP BY the CASE WHEN expression (or its \
positional alias) and include an aggregate for each bucket. Concrete example:
    SELECT
      CASE
        WHEN price < 50  THEN 'Under $50'
        WHEN price < 100 THEN '$50-$99'
        WHEN price < 200 THEN '$100-$199'
        ELSE '$200+'
      END AS price_range,
      COUNT(*) AS order_count
    FROM order_items
    GROUP BY 1
    ORDER BY MIN(price);
- Pivot (long-to-wide): when a question needs one output column per category value \
(e.g. "monthly revenue as a separate column for each product category"), use conditional \
aggregation — a CASE WHEN inside an aggregate function for each target column. Concrete \
example (two category values becoming separate columns):
    SELECT
      month,
      SUM(CASE WHEN category = 'electronics' THEN revenue ELSE 0 END) AS electronics,
      SUM(CASE WHEN category = 'apparel'     THEN revenue ELSE 0 END) AS apparel
    FROM monthly_sales
    GROUP BY month
    ORDER BY month;
- Unpivot (wide-to-long): when multiple metric columns should become rows, use UNION ALL \
with a literal label column to stack them into long format. Concrete example (two metric \
columns unpivoted into rows):
    SELECT 'revenue' AS metric, revenue AS value FROM summary
    UNION ALL
    SELECT 'cost'    AS metric, cost    AS value FROM summary
    ORDER BY metric;
  Adjust source columns, table names, and label strings to match the actual schema.
- Null handling: never substitute 0 for a genuinely missing value. Aggregate \
functions (SUM, AVG, COUNT(col)) already skip NULLs — do not wrap a metric column \
in COALESCE(col, 0) unless the question explicitly asks to treat missing as zero. \
When you filter NULLs out (e.g. WHERE col IS NOT NULL), the filter appears literally \
in the SQL text, which is how it gets extracted and disclosed automatically.
- Null-filter placement for per-category ranking (FIXED PROJECT CONVENTION — same \
rationale as the HAVING COUNT(*) >= 5 rule above): for any query that ranks/compares \
categories by an averaged or rate-based metric, every `col IS NOT NULL` filter on a \
column feeding that metric MUST be applied in ONE WHERE clause in the query's base \
CTE/subquery (the one selecting raw rows), before any GROUP BY — never split across \
multiple stages, and never applied only in a later CTE after an earlier stage already \
aggregated. Splitting or relocating these filters changes which underlying rows count \
toward each group's average, silently producing different row counts and averages for \
the exact same question across separate runs. This is exactly as serious as an \
inconsistent HAVING threshold and must be fixed the same way: one fixed, literal \
placement, every time.
- Unit normalization: when comparing a total or summed metric across groups of \
different sizes (e.g. total revenue by region), prefer a per-unit metric (AVG revenue \
per order) unless the question explicitly asks for totals. Never mix total for one \
group with an average for another in the same result.
- Descriptive aliases only: never use a SQL column alias that implies causation \
(e.g. AS caused_by, AS leads_to). Use factual, descriptive names only.
- Time-filter literalness: when filtering on a date/time column, the date boundaries \
must appear as literal values in the SQL text (string literals, BETWEEN constants, \
date_trunc or EXTRACT expressions) — not hidden inside a subquery — so the time \
range can be read back out of the executed SQL and disclosed to the user automatically.
- Composite-value splitting: when a column in the schema encodes two or more logically \
distinct values in a single field (e.g. a city+state combined as "São Paulo, SP", a \
salary range encoded as "80000-100000", a job title with seniority level appended), and \
the question asks about one component separately, extract that component in the query \
using SPLIT_PART, SUBSTRING, REGEXP_REPLACE, or a CASE WHEN expression rather than \
treating the combined string as one opaque value. Never GROUP BY a combined field when \
the question is about a sub-part of that field."""


def _strip_sql_formatting(text: str) -> str:
    """Strip markdown code fences around a SQL query, if the model added them anyway."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        # Drop the opening fence line (``` or ```sql)
        lines = lines[1:]
        # Drop a trailing fence line if present
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


# ── Deterministic ranking-convention disclosure ─────────────────────────────
#
# Root cause of the "Video Games appears in one run but not the other" bug: a
# question like "which industry should I pursue to maximize both salary and job
# satisfaction" has no single obvious SQL translation — there is no fixed answer
# to "what's the minimum sample size per industry?" or "how do you combine two
# separate metrics into one ranking?" generate_sql is an LLM making that judgment
# call fresh on every single invocation, with nothing constraining it to answer
# the same way twice. One run used HAVING COUNT(*) >= 5 (excluding Video Games,
# which only has 3 job postings); a second run used HAVING COUNT(*) >= 3
# (including it) and also sorted by a different primary metric.
#
# The fix has two parts:
# 1. GENERATE_SQL_SYSTEM_PROMPT now states a FIXED convention (>= 5) so this
#    specific class of query stops varying between runs (see the prompt rule
#    above this function).
# 2. Even fixing the convention doesn't guarantee zero variance forever (a new
#    question shape, a future prompt edit, a different model) — so, following
#    the same lesson already applied to fan-out detection and truncation
#    handling elsewhere in this file, the actual threshold and combination
#    method the executed query relied on are extracted MECHANICALLY from the
#    real SQL text (never re-asked of an LLM, never left to a summarizer's
#    discretion) and appended to final_answer deterministically, every time —
#    so even if some variance remains, the user is never misled about what
#    "top"/"best" specifically meant for that particular run.
_SQL_AST_CACHE: dict = {}


def _parse_sql_ast(sql_query: str):
    """Parse sql_query into a sqlglot AST (Postgres dialect), memoized per query text.

    Returns None when sql_query is empty or fails to parse — every caller treats
    that as "nothing extractable" rather than raising, since these helpers only
    ever run against a query that has already executed successfully (or, in
    tests, a hand-written query that may deliberately be malformed).
    """
    if not sql_query or not sql_query.strip():
        return None
    if sql_query in _SQL_AST_CACHE:
        return _SQL_AST_CACHE[sql_query]
    try:
        tree = sqlglot.parse_one(sql_query, read="postgres")
    except Exception:
        tree = None
    _SQL_AST_CACHE[sql_query] = tree
    return tree


def _extract_min_sample_threshold(sql_query: str):
    """Return the integer threshold N from a HAVING COUNT(*)/COUNT(col) >= N (or > N)
    clause anywhere in the query, or None if no such comparison exists.

    Reads the actual HAVING clause structure from the parsed AST — recognizes
    HAVING COUNT(*) >= 5, HAVING COUNT(order_id) >= 5, HAVING COUNT(*) > 4, and
    any other textual form of the same structural pattern (a COUNT(...) compared
    against an integer literal), not just one fixed regex shape.
    """
    tree = _parse_sql_ast(sql_query)
    if tree is None:
        return None
    for having in tree.find_all(exp.Having):
        for cmp_node in having.find_all((exp.GTE, exp.GT)):
            left, right = cmp_node.this, cmp_node.expression
            if isinstance(left, exp.Count) and isinstance(right, exp.Literal) and right.is_number:
                return right.this
    return None


_RANKING_QUESTION_RE = re.compile(
    r"\b(rank|ranking|compare|comparison|best|worst|top|bottom|highest|lowest)\b",
    re.IGNORECASE,
)
_ALL_GROUPS_REGARDLESS_RE = re.compile(
    r"\b(regardless of size|no matter how (?:few|small)|including small|every group|"
    r"all groups|even (?:small|tiny) (?:categories|groups)|small samples? included)\b",
    re.IGNORECASE,
)


def _query_has_group_by(sql_query: str) -> bool:
    tree = _parse_sql_ast(sql_query)
    if tree is None:
        return False
    return tree.find(exp.Group) is not None


def _query_has_averaged_or_rate_metric(sql_query: str) -> bool:
    """True when the query computes an AVG(...), or a rate (a division where
    either side is itself an aggregate — e.g. SUM(x)/COUNT(*)). The minimum-
    sample-size rule specifically targets averaged/rate-based rankings, not a
    plain SUM/COUNT total, so this deliberately doesn't fire for those.
    """
    tree = _parse_sql_ast(sql_query)
    if tree is None:
        return False
    if tree.find(exp.Avg) is not None:
        return True
    for div in tree.find_all(exp.Div):
        if isinstance(div.this, (exp.Count, exp.Sum, exp.Avg)) or isinstance(
            div.expression, (exp.Count, exp.Sum, exp.Avg)
        ):
            return True
    return False


def _min_sample_rule_violated(question: str, sql_query: str) -> bool:
    """Mechanical, deterministic check for architecture review point #25: the
    minimum-sample-size rule (HAVING COUNT(*) >= 5 for a per-category ranking
    by an averaged/rate metric) was previously only DISCLOSED if present in the
    generated SQL — nothing ever caught the rule being silently skipped
    entirely, since disclosure only extracts what's actually there.

    Fires only when all of the following are true:
    - the question itself uses ranking/comparison language (rank, top, best,
      highest, ...) — this rule doesn't apply to a plain "what's the average
      X per category" with no ranking intent;
    - the question does NOT explicitly ask for every group regardless of size;
    - the generated SQL actually GROUPs BY (a per-category aggregation exists
      at all);
    - that aggregation is averaged/rate-based (_query_has_averaged_or_rate_metric);
    - no HAVING COUNT(*)/COUNT(col) threshold is present anywhere in the query.
    """
    if not sql_query or not question:
        return False
    if not _RANKING_QUESTION_RE.search(question):
        return False
    if _ALL_GROUPS_REGARDLESS_RE.search(question):
        return False
    if not _query_has_group_by(sql_query):
        return False
    if not _query_has_averaged_or_rate_metric(sql_query):
        return False
    return _extract_min_sample_threshold(sql_query) is None


def _order_by_label(order_expr) -> str:
    """Human-readable label for one ORDER BY expression.

    A plain column reference (or an alias reference back to one) is humanized
    the same way column names are elsewhere. Anything structurally richer —
    a CASE expression, a window function, an arithmetic expression — has no
    single "column name" to fall back to, so the real expression text is
    shown verbatim instead of guessing at a label from it.
    """
    if isinstance(order_expr, exp.Column):
        return _humanize_column(order_expr.name)  # type: ignore[name-defined]
    return order_expr.sql(dialect="postgres")


def _extract_order_by_columns(sql_query: str) -> list:
    """Return [(label, direction), ...] for every top-level ORDER BY key, read
    directly from the parsed AST — correctly handling CASE expressions, window
    functions, and multiple sort keys, rather than regex text matching.
    """
    tree = _parse_sql_ast(sql_query)
    if tree is None:
        return []
    # Read the outer query's own "order" arg directly rather than tree.find(exp.Order),
    # which would return the first ORDER BY encountered in document order — including
    # one that belongs to a CTE — instead of the one governing the final result set.
    order = tree.args.get("order") if hasattr(tree, "args") else None
    if order is None:
        order = tree.find(exp.Order)
    if order is None:
        return []
    parsed = []
    for ordered in order.expressions:
        direction = "descending" if ordered.args.get("desc") else "ascending"
        parsed.append((_order_by_label(ordered.this), direction))
    return parsed


# ── New rubric helpers ────────────────────────────────────────────────────────

_NULL_EXCLUSION_COL_RE = re.compile(r"\b(\w+)\s+IS\s+NOT\s+NULL\b", re.IGNORECASE)
_BETWEEN_DATE_RE = re.compile(
    r"\bBETWEEN\s+'([\d\-T: ]+?)'\s+AND\s+'([\d\-T: ]+?)'",
    re.IGNORECASE,
)
_DATE_LITERAL_RE = re.compile(r"'(\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2})?)'")
_DATE_TRUNC_RE = re.compile(r"\bdate_trunc\s*\(\s*'(\w+)'", re.IGNORECASE)
_COUNT_COL_RE = re.compile(
    r"^(?:n|count|num\w*|sample_size|total_count|\w+_count)$", re.IGNORECASE
)
_CAUSAL_PHRASES = re.compile(
    r"\b(?:causes?|caused by|leads? to|results? in|explains? why|responsible for|"
    r"drives?|driven by|because of|due to|impact(?:s|ed)? (?:the|on)|affect(?:s|ed)?)\b",
    re.IGNORECASE,
)
# Ordered list of (pattern, replacement) for clear-cut causal phrases.
# Ambiguous verbs (drives, affects) are intentionally omitted — they have too many
# legitimate non-causal uses and are handled by the disclaimer fallback instead.
_CAUSAL_SUBS = [
    (re.compile(r"\bleads?\s+to\b", re.IGNORECASE), "is associated with"),
    (re.compile(r"\bcauses?\b", re.IGNORECASE), "is associated with"),
    (re.compile(r"\bcaused\s+by\b", re.IGNORECASE), "associated with"),
    (re.compile(r"\bresults?\s+in\b", re.IGNORECASE), "is associated with"),
    (re.compile(r"\bresponsible\s+for\b", re.IGNORECASE), "associated with"),
    (re.compile(r"\bdriven\s+by\b", re.IGNORECASE), "associated with"),
    (re.compile(r"\bexplains?\s+why\b", re.IGNORECASE), "is associated with"),
    (re.compile(r"\bbecause\s+of\b", re.IGNORECASE), "alongside"),
    (re.compile(r"\bdue\s+to\b", re.IGNORECASE), "alongside"),
]
_ASSOCIATION_DISCLAIMER = (
    "Note: this result shows a statistical association only — "
    "the data cannot establish that one variable caused another."
)
_SELECT_DISTINCT_RE = re.compile(r"\bSELECT\s+DISTINCT\b", re.IGNORECASE)


def _extract_null_exclusion_disclosures(sql_query: str) -> list:
    """Return one disclosure string per column that appears in a 'col IS NOT NULL' filter."""
    if not sql_query:
        return []
    seen: set = set()
    disclosures = []
    for col in _NULL_EXCLUSION_COL_RE.findall(sql_query):
        cl = col.lower()
        if cl not in seen:
            seen.add(cl)
            disclosures.append(
                f"Rows where {col.replace('_', ' ').title()} is missing (NULL) "
                f"were excluded from this result."
            )
    return disclosures


def _extract_time_framing_disclosure(sql_query: str) -> str:
    """Return a disclosure when the query filters data to a specific time window."""
    if not sql_query:
        return ""
    m = _BETWEEN_DATE_RE.search(sql_query)
    if m:
        return f"This result covers the period from {m.group(1).strip()} to {m.group(2).strip()}."
    literals = _DATE_LITERAL_RE.findall(sql_query)
    if len(literals) >= 2:
        return f"This result is filtered to the period from {literals[0]} to {literals[-1]}."
    if len(literals) == 1:
        return f"This result is filtered to data around {literals[0]}."
    trunc_m = _DATE_TRUNC_RE.search(sql_query)
    if trunc_m:
        return f"This result is grouped by {trunc_m.group(1)}."
    return ""


def _outlier_sensitivity_note(result_data: list) -> str:
    """Note when one group's metric value is more than 3× the median of the rest.

    Uses _to_float and _humanize_column, which are defined later in this module —
    both are resolved at call time (not import time), so forward references are safe.
    """
    if not result_data or len(result_data) < 3 or not isinstance(result_data[0], dict):
        return ""
    cols = list(result_data[0].keys())
    metric_cols = [c for c in cols[1:] if _to_float(result_data[0].get(c)) is not None]  # type: ignore[name-defined]
    if not metric_cols:
        return ""
    mc = metric_cols[0]
    vals = sorted(
        v for v in (_to_float(r.get(mc)) for r in result_data) if v is not None  # type: ignore[name-defined]
    )
    if len(vals) < 3:
        return ""
    median_val = statistics.median(vals)
    max_val = vals[-1]
    if median_val > 0 and max_val > 3 * median_val:
        label = mc.replace("_", " ").title()
        return (
            f"Note: one or more groups have a {label} value substantially higher than "
            f"the typical group — verify these are not driven by a single extreme data point."
        )
    return ""


def _group_size_imbalance_note(result_data: list) -> str:
    """Note when the largest group has 10× or more rows than the smallest."""
    if not result_data or len(result_data) < 2 or not isinstance(result_data[0], dict):
        return ""
    cols = list(result_data[0].keys())
    count_col = next((c for c in cols if _COUNT_COL_RE.match(c)), None)
    if count_col is None:
        return ""
    counts = [
        _to_float(r.get(count_col)) for r in result_data  # type: ignore[name-defined]
        if _to_float(r.get(count_col)) is not None  # type: ignore[name-defined]
    ]
    if len(counts) < 2 or min(counts) == 0:
        return ""
    ratio = max(counts) / min(counts)
    if ratio >= 10:
        return (
            f"Note: group sizes range from {int(min(counts))} to {int(max(counts))} — "
            f"comparing averages across groups this unevenly sized may underweight "
            f"smaller groups."
        )
    return ""


_MEAN_COL_RE = re.compile(r"^(?:avg|mean)_(?P<base>\w+)$", re.IGNORECASE)


def _significance_test_note(result_data: list) -> str:
    """Rule 15 (Spec 9, Part 3): a real one-way ANOVA across the result's groups
    — replaces "eyeball" heuristics (Rules 5/6) with an actual test statistic
    and p-value for whether the compared groups genuinely differ.

    Only fires when the result itself already contains a mean column
    (avg_<x>/mean_<x>), a matching stddev column (stddev_<x>/std_<x> for the
    SAME <x>), and a count column (_COUNT_COL_RE) — i.e. the three real
    sufficient statistics (mean, sample stddev, n) an ANOVA needs per group.
    No raw per-record data is available at this point (the SQL already
    aggregated it), but ANOVA never needs more than these three numbers per
    group — this produces exactly the same F-statistic a full raw-data ANOVA
    would. Never estimated or guessed when the ingredients aren't present;
    _rubric_applicable_instructions reminds generate_sql to include them for a
    ranking/comparison question, but nothing here fabricates a result when
    they're missing — it just returns "".
    """
    if not result_data or len(result_data) < 2 or not isinstance(result_data[0], dict):
        return ""
    cols = list(result_data[0].keys())

    mean_col = base = None
    for c in cols:
        m = _MEAN_COL_RE.match(c)
        if m:
            mean_col, base = c, m.group("base")
            break
    if mean_col is None:
        return ""

    stddev_col = next(
        (c for c in cols if re.match(rf"^(?:stddev|std)_{re.escape(base)}$", c, re.IGNORECASE)),
        None,
    )
    count_col = next((c for c in cols if _COUNT_COL_RE.match(c)), None)
    if stddev_col is None or count_col is None:
        return ""

    groups = []
    for row in result_data:
        mean_v = _to_float(row.get(mean_col))  # type: ignore[name-defined]
        std_v = _to_float(row.get(stddev_col))  # type: ignore[name-defined]
        n_v = _to_float(row.get(count_col))  # type: ignore[name-defined]
        if mean_v is None or std_v is None or n_v is None or n_v < 1:
            continue
        groups.append((mean_v, std_v, int(n_v)))
    if len(groups) < 2:
        return ""

    k = len(groups)
    total_n = sum(n for _, _, n in groups)
    df_between = k - 1
    df_within = total_n - k
    if df_within <= 0:
        return ""

    grand_mean = sum(mean_v * n for mean_v, _, n in groups) / total_n
    ssb = sum(n * (mean_v - grand_mean) ** 2 for mean_v, _, n in groups)
    ssw = sum((n - 1) * (std_v ** 2) for _, std_v, n in groups if n > 1)
    if ssw <= 0:
        return ""

    msb = ssb / df_between
    msw = ssw / df_within
    if msw == 0:
        return ""
    f_stat = msb / msw

    from scipy import stats as _scipy_stats

    p_value = float(_scipy_stats.f.sf(f_stat, df_between, df_within))
    label = _humanize_column(base)  # type: ignore[name-defined]

    if p_value < 0.05:
        return (
            f"A one-way ANOVA on {label} across these {k} groups found a statistically "
            f"significant difference (F={f_stat:.2f}, p={p_value:.4f}) — unlikely to be "
            f"due to chance alone."
        )
    return (
        f"A one-way ANOVA on {label} across these {k} groups found no statistically "
        f"significant difference (F={f_stat:.2f}, p={p_value:.4f}) — the observed "
        f"differences could plausibly be due to normal variation rather than a real effect."
    )


def _rubric_applicable_instructions(question: str) -> str:
    """Return extra prompt instructions for generate_sql based on keywords in the question.

    These supplement the fixed system-prompt rules with question-specific reminders,
    appended to generate_sql's human_content. Only fires when the question explicitly
    invokes the relevant pattern — most questions get nothing added.
    """
    q = question.lower()
    notes = []

    if re.search(r"\b(why|cause|causes|because|explain|reason|lead|result|impact|effect|affect)\b", q):
        notes.append(
            "RUBRIC NOTE: this question implies a causal relationship. SQL results show "
            "association only — use purely descriptive language in column aliases and comments."
        )

    if re.search(r"\b(average|avg|mean|highest|lowest|best|worst|top|bottom|rate|per )\b", q):
        notes.append(
            "RUBRIC NOTE: this question compares metrics across categories. Apply "
            "HAVING COUNT(*) >= 5 when ranking by an average or rate. Prefer per-unit "
            "metrics (avg per entity) over raw totals when groups have different sizes. "
            "Also SELECT STDDEV(...) and COUNT(...) for the compared metric alongside its "
            "AVG(...) (e.g. avg_rating, stddev_rating, count) so a real significance test "
            "can check whether the groups actually differ, not just compare bare averages."
        )

    if re.search(r"\b(since|between|from \d|in \d{4}|year|month|quarter|recent|last \d)\b", q):
        notes.append(
            "RUBRIC NOTE: this question implies a time scope. Any date filter must appear "
            "literally in the SQL WHERE clause so the time range can be disclosed automatically."
        )

    return "\n\n".join(notes)


def _analyst_judgment_disclosure(sql_query: str, result_data=None) -> str:
    """Unified, mechanically-derived disclosure of every judgment call baked into the
    executed SQL. Replaces _ranking_convention_disclosure() and extends it with four
    additional categories (null exclusion, time framing, outlier sensitivity, group
    size imbalance).

    Returns "" when no disclosure applies — most queries don't trigger any rule.
    Built entirely from the real SQL text and parsed result rows; never re-asked of
    an LLM and never invented when the pattern is absent.
    """
    if not sql_query:
        return ""

    notes = []

    # Rule 1: minimum sample threshold
    n = _extract_min_sample_threshold(sql_query)
    if n is not None:
        notes.append(
            f"Only groups with at least {n} underlying rows were included in this "
            f"ranking (smaller groups were excluded to avoid basing an average on "
            f"too little data)."
        )

    # Rule 2: combined-metric ranking (only disclose when 2+ ORDER BY columns)
    parsed = _extract_order_by_columns(sql_query)
    if len(parsed) >= 2:
        primary, *rest = parsed
        desc = f"ranked primarily by {primary[0]} ({primary[1]})"
        for label, direction in rest:
            desc += f", then by {label} ({direction})"
        notes.append(f"This result is {desc}.")

    # Rule 3: null exclusions
    notes.extend(_extract_null_exclusion_disclosures(sql_query))

    # Rule 4: time framing
    time_note = _extract_time_framing_disclosure(sql_query)
    if time_note:
        notes.append(time_note)

    # Rule 12: deduplication transparency
    dedup_note = _extract_deduplication_disclosure(sql_query)
    if dedup_note:
        notes.append(dedup_note)

    # Rules 5 & 6: outlier and group-size checks (require parsed result data)
    if result_data:
        outlier_note = _outlier_sensitivity_note(result_data)
        if outlier_note:
            notes.append(outlier_note)
        imbalance_note = _group_size_imbalance_note(result_data)
        if imbalance_note:
            notes.append(imbalance_note)
        # Rule 15 (Spec 9, Part 3): real ANOVA significance test, only when the
        # result already carries the mean/stddev/count ingredients it needs.
        significance_note = _significance_test_note(result_data)
        if significance_note:
            notes.append(significance_note)

    return " ".join(notes)


def _extract_deduplication_disclosure(sql_query: str) -> str:
    """Disclose when the query removes duplicate rows via SELECT DISTINCT.

    SELECT DISTINCT silently assumes that repeated identical rows are true
    duplicates (data-entry artefacts) rather than legitimate repeated
    observations (e.g. a customer placing two identical orders). That
    assumption is correct in many cases but is never obvious from the result
    alone, so it is disclosed here when the pattern is present in the SQL.
    """
    if not sql_query:
        return ""
    if _SELECT_DISTINCT_RE.search(sql_query):
        return (
            "Duplicate rows were removed from this result (SELECT DISTINCT was used) — "
            "this assumes repeated identical rows are true duplicates, not legitimate "
            "repeated observations."
        )
    return ""


def _apply_causal_correction(final_answer: str, _sql_query: str) -> str:
    """Rewrite causal language in the final answer to associative language.

    Two-pass approach:
    1. Replace unambiguously causal phrases in-place with associative equivalents
       (e.g. 'leads to' → 'is associated with'). Ambiguous verbs ('drives',
       'affects') are left for the fallback because they have too many
       legitimate non-causal uses.
    2. If any causal phrase remains after rewriting (the ambiguous cases),
       append the association disclaimer as a fallback.

    This is intentionally a rewrite, not just a disclaimer bolted on: leaving
    causal phrasing in the answer while appending a correction below it means
    a reader who stops reading early still sees the false claim.
    """
    text = final_answer
    for pattern, replacement in _CAUSAL_SUBS:
        text = pattern.sub(replacement, text)
    if _CAUSAL_PHRASES.search(text) and _ASSOCIATION_DISCLAIMER not in text:
        text = f"{text}\n\n{_ASSOCIATION_DISCLAIMER}"
    return text


_CHART_SQL_SHAPING: dict = {
    "line": (
        "GROUP BY the relevant time period and ORDER BY it chronologically. "
        "Return exactly one row per time period so the chart has a clean x-axis."
    ),
    "bar": (
        "GROUP BY the category, aggregate the metric (e.g. SUM, COUNT, or AVG), "
        "and ORDER BY the aggregate. Consider a reasonable LIMIT (e.g. TOP 10 or TOP 20) "
        "if there are many categories."
    ),
    "scatter": (
        "Identify what the 'entity' being plotted actually is. If each point is meant to "
        "be an individual raw record (a single transaction, order, job posting, etc.), "
        "return the two raw numeric columns unaggregated, one row per record — do not "
        "GROUP BY. But if each point is meant to be a GROUP or CATEGORY (e.g. one point "
        "per industry, region, product category, customer segment), you MUST GROUP BY "
        "that category and aggregate both numeric measures (e.g. AVG, COUNT, percentage) "
        "down to exactly one row per category first — returning raw unaggregated rows in "
        "that case produces a meaningless plot (hundreds of individual records/duplicated "
        "category labels instead of one point per category) and silently truncates before "
        "covering every category. When the question explicitly asks to compare categories "
        "or groups against each other (not individual records), always aggregate to one "
        "row per group."
    ),
    "pie": (
        "GROUP BY the category and aggregate. Cap at 5 categories maximum — "
        "use a LIMIT or roll up smaller categories into 'Other'."
    ),
    "donut": (
        "GROUP BY the category and aggregate. Cap at 5 categories maximum — "
        "use a LIMIT or roll up smaller categories into 'Other'."
    ),
    "histogram": (
        "Return the raw values of the single variable being distributed, one row per entity. "
        "Do not pre-bucket — the chart engine bins the values."
    ),
    "box": (
        "Return the raw values of the measured variable plus the grouping column, "
        "one row per entity. Do not pre-aggregate."
    ),
    "stacked bar": (
        "GROUP BY both the main category (x-axis) and the sub-category (the stack dimension), "
        "with the aggregate value. Return one row per (category, sub-category) pair."
    ),
    "treemap": (
        "GROUP BY the outer (parent) category first, then the inner (child) sub-category. "
        "Include an aggregate (e.g. SUM or COUNT) for the size dimension. "
        "Return exactly one row per (outer_category, inner_sub_category) pair — "
        "the two label columns plus the aggregate value."
    ),
}


def _chart_shaping_instruction(chart_type: str) -> str:
    """Return the SQL-shaping instruction for a given chart type.

    Matches on any key that appears as a substring in the lowercased chart_type,
    so "horizontal bar chart" -> bar, "line graph" -> line, etc.
    Falls back to a generic instruction if nothing matches.
    """
    ct = chart_type.lower()
    # Check stacked bar before plain bar so it doesn't match bar first.
    for key in ("stacked bar", "line", "bar", "scatter", "donut", "pie", "histogram", "box", "treemap"):
        if key in ct:
            return _CHART_SQL_SHAPING[key]
    return (
        "Shape the query to return data appropriate for the chart type. "
        "Use GROUP BY and ORDER BY as needed."
    )


def generate_sql(state: SQLAnalystState) -> dict:
    """Node 3 (high tier): write exactly one SQL query from curated_question + context.

    If this is a retry after a failed execution (sql_query_execution_result holds an
    error), that error is included so the model can try again with real information
    about what went wrong.

    When wants_visualization is True, the prompt is extended with the determined
    chart_type and chart-type-specific shaping instructions so the query result
    structure matches what a chart renderer expects. The normal (non-visualization)
    path is completely unchanged.

    DELIBERATELY OUT OF SCOPE — do not add these here:
    - Data-cleaning operations (trimming whitespace, normalising casing, filling nulls,
      replacing placeholder values, deduplicating rows): that is clean_dataset()'s job,
      which runs at load time. This function assumes it is querying already-clean data.
    - Building a persistent, multi-table data model with a relationship layer (the
      equivalent of Power BI's DAX model or Tableau's calculated fields/relationships).
      Each invocation produces one flat, correctly-shaped result for one specific chart
      or question — not an interconnected model with cross-table calculated metrics.
      A genuinely complex multi-table dashboard is BI-tool development work and is
      outside what this system does.
    """
    llm = pick_llm("high")

    human_content = (
        f"Question: {state.curated_question}\n\n"
        f"Database context:\n{state.prompt_query_context}"
    )

    if state.wants_visualization and state.chart_type:
        shaping = _chart_shaping_instruction(state.chart_type)
        human_content += (
            f"\n\nThis query will supply data for a {state.chart_type} chart. "
            f"Shape the query accordingly: {shaping}"
        )

    rubric_notes = _rubric_applicable_instructions(state.curated_question)
    if rubric_notes:
        human_content += f"\n\n{rubric_notes}"

    if state.sql_query_execution_result:
        human_content += (
            "\n\nA previous attempt at this query failed with this real database error "
            f"— fix the query so it actually works:\n{state.sql_query_execution_result}"
            f"\n\nPrevious (failed) query was:\n{state.generated_sql_query}"
        )

    response = llm.invoke(
        [
            ("system", GENERATE_SQL_SYSTEM_PROMPT),
            ("human", human_content),
        ]
    )
    sql_query = _strip_sql_formatting(_extract_text(response.content))

    # Mechanical compliance check (architecture review point #25): the minimum-
    # sample-size rule was previously only disclosed if present, never enforced
    # — a first attempt that silently skipped it entirely would execute
    # unchallenged. Regenerate exactly once, with the missing rule made
    # explicit, the same corrective pattern already used for real execution
    # errors above (never re-ask an LLM to "double check" — feed it the
    # concrete violation and the previous query, same as a real DB error).
    if _min_sample_rule_violated(state.curated_question, sql_query):
        retry_content = (
            human_content
            + "\n\nCOMPLIANCE CHECK FAILED: your previous query grouped by category and "
            "ranked/compared an averaged or rate-based metric, but did not include the "
            "required HAVING COUNT(*) >= 5 (or equivalent) minimum-sample-size clause — "
            "this is a fixed project convention (see the rules above), not optional. "
            "Add it now.\n\n"
            f"Previous (non-compliant) query was:\n{sql_query}"
        )
        retry_response = llm.invoke(
            [
                ("system", GENERATE_SQL_SYSTEM_PROMPT),
                ("human", retry_content),
            ]
        )
        sql_query = _strip_sql_formatting(_extract_text(retry_response.content))

    return {"generated_sql_query": sql_query}


IS_SAFE_SYSTEM_PROMPT = """You are a security judge reviewing a SQL query. Your ONLY job is \
to decide whether this query is strictly read-only.

Answer "no" if the query contains, anywhere in it, any of: INSERT, UPDATE, DELETE, DROP, \
ALTER, TRUNCATE (in any case, including inside subqueries, CTEs, or comments). Otherwise \
answer "yes".

Do not evaluate correctness, style, or performance — only read-only safety. Give a brief \
reason in comments."""


# Top-level statement types that represent a genuinely read-only query. exp.SetOperation
# covers UNION/INTERSECT/EXCEPT; exp.Subquery covers a query wrapped in extra parens
# (e.g. "(SELECT 1)"). A WITH ... SELECT CTE parses as exp.Select directly (the CTE
# definitions live under its own "with" arg), so no separate exp.With case is needed.
_READONLY_STATEMENT_TYPES = (exp.Select, exp.SetOperation, exp.Subquery)

# Any of these appearing ANYWHERE in an otherwise-read-shaped statement (e.g. nested in
# a CTE body, or smuggled into a subquery) is rejected outright — defense in depth on
# top of the top-level-statement-type check above.
_WRITE_NODE_TYPES = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Alter,
    exp.TruncateTable, exp.Create, exp.Merge,
)


def _ast_safety_check(sql_query: str) -> tuple[bool, str]:
    """Deterministic, authoritative SQL safety gate (architecture review point #23).

    Replaces keyword/LLM-judgment-style safety review, which is foolable by comment
    tricks, unusual casing/encoding, or a semicolon-chained second statement (psycopg2
    happily executes multiple ';'-separated statements in one call — see execute_sql).
    sqlglot.parse_one() alone does NOT reject multi-statement input: it silently wraps
    everything into one exp.Block node (confirmed: "SELECT 1; DROP TABLE foo;" parses
    without error). This uses sqlglot.parse() instead, which returns one AST per
    semicolon-separated statement, so multi-statement input is caught structurally.

    Enforces:
      1. The query must parse as valid SQL at all.
      2. Exactly one statement (ignoring only genuinely empty ones from stray trailing
         semicolons, e.g. "SELECT 1;;").
      3. That one statement must be a SELECT / WITH-...-SELECT CTE / set operation
         (UNION/INTERSECT/EXCEPT) / parenthesized subquery — see
         _READONLY_STATEMENT_TYPES.
      4. No "SELECT ... INTO <table>" (Postgres' shorthand for creating a new table
         from a query result — a write disguised as a SELECT).
      5. No write/DDL node anywhere in the tree (defense in depth for a write smuggled
         inside a CTE body or subquery under an outer SELECT).

    Returns (is_safe, reason). reason is "" when is_safe is True.
    """
    text = (sql_query or "").strip()
    if not text:
        return False, "Query is empty."

    try:
        statements = [s for s in sqlglot.parse(text, read="postgres") if s is not None]
    except Exception as e:
        return False, f"Query could not be parsed as valid SQL: {e}"

    if len(statements) != 1:
        return False, (
            f"Exactly one SQL statement is allowed; found {len(statements)} "
            "(a semicolon-separated multi-statement query is rejected outright)."
        )

    stmt = statements[0]
    if not isinstance(stmt, _READONLY_STATEMENT_TYPES):
        return False, (
            f"Only read-only SELECT queries are allowed; found a "
            f"{type(stmt).__name__} statement."
        )

    if isinstance(stmt, exp.Select) and stmt.args.get("into") is not None:
        return False, "SELECT ... INTO is not allowed — it creates a table."

    for node in stmt.walk():
        if isinstance(node, _WRITE_NODE_TYPES):
            return False, f"Query contains a disallowed {type(node).__name__} operation."

    return True, ""


def is_safe(state: SQLAnalystState) -> dict:
    """Node 4: deterministic AST safety gate (authoritative), then a cheap-tier LLM
    call (secondary, non-authoritative sanity check whose comments are surfaced for
    transparency but never override an AST-cleared query back to unsafe).

    The AST check (_ast_safety_check) is the real security boundary here — see its
    docstring for why the LLM alone was foolable (comment tricks, encoding, a
    semicolon-chained second statement). Receives ONLY the generated SQL text —
    never the original or curated question.
    """
    sql_query = state.generated_sql_query
    ast_is_safe, ast_reason = _ast_safety_check(sql_query)
    if not ast_is_safe:
        return {
            "is_safe": "no",
            "comments": f"Rejected by deterministic SQL safety check: {ast_reason}",
        }

    llm = pick_llm("cheap").with_structured_output(JudgeSchema)
    judgement: JudgeSchema = llm.invoke(
        [
            ("system", IS_SAFE_SYSTEM_PROMPT),
            ("human", sql_query),
        ]
    )

    return {"is_safe": "yes", "comments": judgement.comments}


def route_after_safety_check(state: SQLAnalystState) -> str:
    """Conditional edge function for after is_safe.

    Returns a plain string key directly ("execute_sql" or "cancel_sql") based on
    state.is_safe. This return value is passed straight into add_conditional_edges'
    routing mapping — it is NOT written into shared state and read back out, which
    is what would cause a LangGraph concurrent-update error in this graph.
    """
    if state.is_safe == "yes":
        return "execute_sql"
    return "cancel_sql"


MAX_SQL_ATTEMPTS = 5
_SQL_ERROR_PREFIX = "SQL_EXECUTION_ERROR: "
MAX_RESULT_ROWS = 200
# Statement timeout applied on every app_reader connection before executing user SQL.
# Guards against queries that must fully execute before returning any rows — the
# MAX_RESULT_ROWS cap alone cannot stop those. Units: milliseconds.
_STATEMENT_TIMEOUT_MS = 30_000


class _ResultEncoder(json.JSONEncoder):
    """Serialize psycopg2 result types that are not native JSON."""

    def default(self, obj):
        if isinstance(obj, _decimal.Decimal):
            return float(obj)
        if isinstance(obj, (_dt.datetime, _dt.date)):
            return obj.isoformat()
        return super().default(obj)


def execute_sql(state: SQLAnalystState) -> dict:
    """Node 6: run generated_sql_query against Postgres using the app_reader role.

    On a real database error (bad column, syntax error, etc.) it does not crash —
    it captures the exact error message (prefixed with a sentinel so the routing
    function can distinguish it deterministically from a real result string) and
    increments the attempt counter so the conditional edge after this node can
    route back to generate_sql with that error, capped at MAX_SQL_ATTEMPTS total
    attempts across the whole cycle.

    Result rows are capped at MAX_RESULT_ROWS and serialized as structured JSON
    {"columns": [...], "rows": [[...], ...], "truncated": bool}, with Decimal and
    datetime values converted at serialization time so downstream parsing requires
    only json.loads — no eval(), no regex reconstruction.

    A statement_timeout is set on every connection as a hard resource safeguard:
    a query that must fully execute before returning any rows cannot be stopped
    by the row cap alone, so we bound its wall-clock time at the database level.
    """
    attempts = state.sql_attempts + 1
    conn = get_app_reader_connection()
    try:
        # SET LOCAL statement_timeout only binds within an open transaction block —
        # under autocommit=True, each cur.execute() is its own implicit transaction,
        # so a timeout set on one statement would never apply to the next (the
        # query below would run completely unbounded). psycopg2 connections default
        # to autocommit=False (get_app_reader_connection never sets it), which is
        # what makes SET LOCAL here + the query below share one transaction — but
        # this is verified explicitly, not just assumed, so a future change to the
        # connection helper can't silently reopen this hole.
        if conn.autocommit:
            conn.autocommit = False
        with conn.cursor() as cur:
            # Hard resource guard: kill the query if it runs longer than the timeout.
            # SET LOCAL applies for the current transaction only.
            cur.execute("SET LOCAL statement_timeout = %s", (_STATEMENT_TIMEOUT_MS,))
            # generated_sql_query is model-authored SQL text, not a value to bind —
            # there is no placeholder mechanism for "run this arbitrary statement";
            # safety here is enforced upstream by the is_safe judge gate, not by
            # parameterization (which doesn't apply to whole-statement execution).
            cur.execute(state.generated_sql_query)
            if cur.description is not None:
                col_names = [desc[0] for desc in cur.description]
                rows = cur.fetchmany(MAX_RESULT_ROWS + 1)
                truncated = len(rows) > MAX_RESULT_ROWS
                if truncated:
                    rows = rows[:MAX_RESULT_ROWS]
                result_payload = {
                    "columns": col_names,
                    "rows": [list(r) for r in rows],
                    "truncated": truncated,
                }
                result_str = json.dumps(result_payload, cls=_ResultEncoder)
            else:
                result_str = "(query executed, no rows returned)"
        # Read-only connection: roll back the implicit transaction cleanly rather
        # than committing (there is nothing to commit on a SELECT).
        conn.rollback()
        return {"sql_query_execution_result": result_str, "sql_attempts": attempts}
    except Exception as e:
        conn.rollback()
        error_str = f"{_SQL_ERROR_PREFIX}{type(e).__name__}: {e}"
        if attempts >= MAX_SQL_ATTEMPTS:
            return {
                "sql_query_execution_result": error_str,
                "sql_attempts": attempts,
                "final_answer": (
                    f"The query could not be completed after {attempts} attempts. "
                    f"The last real database error was: {type(e).__name__}: {e}"
                ),
            }
        return {"sql_query_execution_result": error_str, "sql_attempts": attempts}
    finally:
        conn.close()


def route_after_execute_sql(state: SQLAnalystState) -> str:
    """Conditional edge function for after execute_sql.

    Returns a plain string key: "represent_final_answer" on the exhausted-retry
    error case (final_answer already set by execute_sql), "generate_sql" on a
    retryable error, "resolve_chart_columns" on success when wants_visualization
    is True (which then flows on to validate_chart_shape -> build_visualization),
    and "represent_final_answer" on success for a normal question.
    """
    if state.final_answer:
        # execute_sql already gave up after MAX_SQL_ATTEMPTS — pass the error
        # through represent_final_answer unchanged, regardless of wants_visualization.
        return "represent_final_answer"
    if state.sql_query_execution_result.startswith(_SQL_ERROR_PREFIX):
        return "generate_sql"
    if state.wants_visualization:
        return "resolve_chart_columns"
    return "represent_final_answer"


def cancel_sql(state: SQLAnalystState) -> dict:
    """Node 7: write a final_answer explaining the query was blocked, quoting the
    judge's own comments as the reason. Appends this as an AIMessage to messages.
    """
    final_answer = (
        "This query was blocked before execution because it did not pass the "
        f"read-only safety check. Reason given by the safety judge: {state.comments}"
    )
    return {
        "final_answer": final_answer,
        "messages": [AIMessage(content=final_answer)],
    }


REPRESENT_FINAL_ANSWER_SYSTEM_PROMPT = """You take a raw SQL execution result and the \
original question, and write a plain-English answer to what was actually asked.

Rules:
- No SQL, no raw column names dumped as-is — translate into natural language.
- If the execution result is empty, or doesn't clearly answer the question, say so \
plainly instead of forcing an answer.
- Be concise and directly answer the question.
- CRITICAL: only report what the execution result actually shows. The user's original \
question may ask for an action (an update, a deletion, sending something, etc.) that was \
never performed — the SQL that ran may have been read-only (e.g. a SELECT), or blocked \
entirely. Never claim, imply, or hint that any change, update, or write happened unless the \
raw execution result itself explicitly reflects it. If the question asked for an action \
that clearly did not occur, say plainly that only the requested data was retrieved and no \
change was made — do not agree that it happened just because the user asked for it.
- Data-quality note: if the "Data quality notes" section below is non-empty, it means at \
least one table this query actually touched either has no recorded data-quality check, or \
has an unresolved critical (fail-level) data-quality issue. State this plainly but briefly \
— one clear sentence, not alarming — e.g. "Note: this data has an unresolved quality issue \
and the result may be affected." or "Note: this table has never been checked for data \
quality." Do this ONLY when that section is actually non-empty; if it's empty, say nothing \
about data quality at all."""


def _extract_referenced_tables(sql_query: str) -> set:
    """Real table references anywhere in the query — including inside subqueries
    and CTE bodies — as lowercase names, read from the parsed AST rather than a
    text search. A string literal or comment that happens to contain a table
    name never matches, since it never becomes an exp.Table node; a CTE's own
    name (which also parses as an exp.Table when it's later selected FROM) is
    excluded so it can't be mistaken for a real table reference.
    """
    tree = _parse_sql_ast(sql_query)
    if tree is None:
        return set()
    cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    return {
        t.name.lower()
        for t in tree.find_all(exp.Table)
        if t.name.lower() not in cte_names
    }


def _query_touches_table(sql_query: str, table_name: str) -> bool:
    """Whether table_name is a real table reference anywhere in the executed SQL
    (including inside subqueries and CTEs) — case-insensitive (Postgres folds
    unquoted identifiers to lowercase, and table_name here always comes from
    information_schema, already lowercase).
    """
    if not sql_query:
        return False
    return table_name.lower() in _extract_referenced_tables(sql_query)


def _relevant_data_quality_notes(state: SQLAnalystState) -> list:
    """Filter state.data_quality_warnings (fail-level + no-record only, never
    warn-level — add_context never puts warn-level entries in this list at all) down
    to just the tables the actually-generated/executed query touches, by name-matching
    against generated_sql_query. A warning about a table this specific query never
    referenced would be noise unrelated to this answer, so it's excluded here even
    though it's true and present in the wider schema context."""
    if not state.data_quality_warnings or not state.generated_sql_query:
        return []
    return [
        w["warning"]
        for w in state.data_quality_warnings
        if _query_touches_table(state.generated_sql_query, w["table"])
    ]


def represent_final_answer(state: SQLAnalystState) -> dict:
    """Node 8 (low tier): turn the raw execution result into a plain-English answer.

    Appends the final answer as an AIMessage to messages.
    """
    # If execute_sql already gave up after exhausting retries, it already wrote a
    # final_answer explaining the failure — don't overwrite it with an LLM guess.
    if state.final_answer:
        return {
            "final_answer": state.final_answer,
            "messages": [AIMessage(content=state.final_answer)],
        }

    # Parse once; use result_data for both the LLM content and the disclosure helpers.
    result_data, was_truncated = _parse_sql_result(state.sql_query_execution_result)

    # Deterministic guard, not just a prompt instruction: a small local model was
    # observed (live, reproduced in tests/test_result_truncation.py) to fabricate
    # counts/averages/min/max over a truncated result anyway, despite an explicit
    # system-prompt rule not to. Rather than trust the LLM to comply, skip the LLM
    # summarization entirely when the result is truncated and report the
    # limitation directly — this cannot be talked out of by the model.
    if was_truncated:
        final_answer = (
            "This query matched more rows than could be retrieved in full "
            f"(results are capped at {MAX_RESULT_ROWS} rows), so I can't give you a complete "
            "answer over the full result set — only a partial sample was available. "
            "Try narrowing the question (e.g. ask for a top-N, a specific filter, or an "
            "aggregate computed directly in the query) so the database can return a complete, "
            "summarizable answer."
        )
        return {
            "final_answer": final_answer,
            "messages": [AIMessage(content=final_answer)],
        }

    relevant_notes = _relevant_data_quality_notes(state)
    data_quality_section = "\n".join(relevant_notes) if relevant_notes else "(none)"

    llm = pick_llm("cheap")
    human_content = (
        f"Original question: {state.user_question}\n\n"
        f"The SQL query that was actually executed:\n{state.generated_sql_query}\n\n"
        f"Raw SQL execution result: {result_data}\n\n"
        f"Data quality notes for tables this query touched:\n{data_quality_section}"
    )
    response = llm.invoke(
        [
            ("system", REPRESENT_FINAL_ANSWER_SYSTEM_PROMPT),
            ("human", human_content),
        ]
    )
    final_answer = _extract_text(response.content).strip()

    # Deterministic, mandatory transparency — not just a prompt instruction, since
    # prompt instructions alone have already proven unreliable in this project
    # (see the truncation-fabrication and question-paraphrasing bugs). Every
    # triggered rubric rule is extracted mechanically from the real query text and
    # parsed result data — the LLM summarizer cannot omit or reword it away.
    disclosure = _analyst_judgment_disclosure(state.generated_sql_query, result_data)
    if disclosure:
        final_answer = f"{final_answer}\n\nHow this answer was computed: {disclosure}"
    final_answer = _apply_causal_correction(final_answer, state.generated_sql_query)

    return {
        "final_answer": final_answer,
        "messages": [AIMessage(content=final_answer)],
    }


def _humanize_column(col: str) -> str:
    """Convert a SQL alias like 'avg_payment_value' -> 'Average Payment Value'."""
    return col.replace("_", " ").title()


# ── Chart image rendering ────────────────────────────────────────────────────


def _to_float(val):
    """Safely coerce any value to float; return None if not possible."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _numeric_cols(data: list, cols: list) -> list:
    """Return subset of cols whose first non-None value parses as float."""
    if not data:
        return []
    first = data[0]
    return [c for c in cols if _to_float(first.get(c)) is not None]


def _chart_bar(ax, data: list, cols: list, category_col: str = "", value_col: str = "") -> None:
    """Draw a bar per row. A genuinely NULL metric value is drawn as a 0-height
    bar but marked with a hatch pattern + "No data" label so it is never
    visually indistinguishable from a real zero (which renders as a plain,
    unmarked 0-height bar).

    category_col/value_col, when set and present in cols, are the real columns
    the question actually meant (resolved by resolve_chart_columns). When not
    set, falls back to the original positional behavior (cols[0]/cols[1])."""
    cat_col = category_col if category_col and category_col in cols else cols[0]
    val_col = value_col if value_col and value_col in cols else (cols[1] if len(cols) >= 2 else None)
    x_labels = [str(r[cat_col]) for r in data]
    if val_col is not None:
        raw_vals = [_to_float(r[val_col]) for r in data]
    else:
        raw_vals = list(range(len(data)))
    pos = list(range(len(x_labels)))
    heights = [0.0 if v is None else v for v in raw_vals]
    bars = ax.bar(pos, heights)
    if val_col is not None:
        for i, v in enumerate(raw_vals):
            if v is None:
                bars[i].set_hatch("//")
                bars[i].set_edgecolor("gray")
                bars[i].set_facecolor("none")
                ax.annotate(
                    "No data", (pos[i], 0), ha="center", va="bottom",
                    fontsize=7, color="gray", rotation=90,
                )
    ax.set_xticks(pos)
    ax.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=8)
    ax.set_xlabel(_humanize_column(cat_col))
    if val_col is not None:
        ax.set_ylabel(_humanize_column(val_col))


def _chart_line(ax, data: list, cols: list, category_col: str = "", value_col: str = "") -> None:
    """Draw the line. A genuinely NULL metric value is passed through as NaN,
    which matplotlib renders as a real gap in the line (no segment drawn
    through it, no marker plotted there) — distinct from a real zero, which
    draws a marker at y=0.

    category_col/value_col: see _chart_bar's docstring — same resolution/fallback
    precedence."""
    cat_col = category_col if category_col and category_col in cols else cols[0]
    val_col = value_col if value_col and value_col in cols else (cols[1] if len(cols) >= 2 else None)
    x_labels = [str(r[cat_col]) for r in data]
    if val_col is not None:
        y_vals = [_to_float(r[val_col]) for r in data]
        y_vals = [float("nan") if v is None else v for v in y_vals]
    else:
        y_vals = []
    pos = list(range(len(x_labels)))
    ax.plot(pos, y_vals, marker="o", linewidth=2, markersize=4)
    ax.set_xticks(pos)
    step = max(1, len(x_labels) // 12)
    ax.set_xticklabels(
        [lbl if i % step == 0 else "" for i, lbl in enumerate(x_labels)],
        rotation=45, ha="right", fontsize=8,
    )
    ax.set_xlabel(_humanize_column(cat_col))
    if val_col is not None:
        ax.set_ylabel(_humanize_column(val_col))


def _chart_scatter(ax, data: list, cols: list, category_col: str = "", value_col: str = "") -> None:
    """category_col/value_col, when both set, present in cols, AND both numeric,
    are used as the explicit x/y measures (the two real numeric columns the
    question actually meant). Otherwise falls back to the original
    numeric-detection behavior."""
    x_col = y_col = None
    if (
        category_col and category_col in cols and _to_float(data[0].get(category_col)) is not None
        and value_col and value_col in cols and _to_float(data[0].get(value_col)) is not None
        and category_col != value_col
    ):
        x_col, y_col = category_col, value_col
        label_col = next((c for c in cols if c not in (x_col, y_col)), None)
    else:
        num = _numeric_cols(data, cols)
        str_cols = [c for c in cols if c not in num]
        if len(num) >= 2:
            x_col, y_col = num[0], num[1]
            label_col = str_cols[0] if str_cols else None
        elif len(cols) >= 2:
            x_col, y_col = cols[-2], cols[-1]
            label_col = None
        else:
            return
    xs = [_to_float(r[x_col]) for r in data]
    ys = [_to_float(r[y_col]) for r in data]
    valid_xs = [x for x, y in zip(xs, ys) if x is not None and y is not None]
    valid_ys = [y for x, y in zip(xs, ys) if x is not None and y is not None]
    if not valid_xs:
        return
    ax.scatter(valid_xs, valid_ys, alpha=0.7, s=60)
    if label_col:
        for i, row in enumerate(data):
            xi, yi = _to_float(row[x_col]), _to_float(row[y_col])
            if xi is not None and yi is not None:
                ax.annotate(str(row[label_col]), (xi, yi), fontsize=6, alpha=0.8,
                            xytext=(3, 3), textcoords="offset points")
    ax.set_xlabel(_humanize_column(x_col))
    ax.set_ylabel(_humanize_column(y_col))


def _chart_pie(
    ax, data: list, cols: list, donut: bool = False,
    category_col: str = "", value_col: str = "",
) -> None:
    """A pie/donut wedge has no way to represent "missing" as opposed to a
    real, tiny-but-present zero share — a 0-value wedge is already invisible,
    so silently including a NULL as 0 would be indistinguishable from a real
    zero. Instead, rows with a NULL metric are omitted from the chart
    entirely rather than plotted as a same-looking zero wedge.

    category_col/value_col: see _chart_bar's docstring — same resolution/fallback
    precedence."""
    if len(cols) < 2:
        return
    cat_col = category_col if category_col and category_col in cols else cols[0]
    val_col = value_col if value_col and value_col in cols else cols[1]
    pairs = [(str(r[cat_col]), _to_float(r[val_col])) for r in data]
    pairs = [(label, val) for label, val in pairs if val is not None]
    if not pairs:
        return
    labels = [label for label, _ in pairs]
    vals = [abs(val) for _, val in pairs]
    if sum(vals) == 0:
        return
    wedge_kw = {"width": 0.5} if donut else {}
    ax.pie(vals, labels=labels, autopct="%1.1f%%", wedgeprops=wedge_kw)


def _chart_histogram(ax, data: list, cols: list, category_col: str = "", value_col: str = "") -> None:
    """value_col, when set, present in cols, and numeric, is the variable being
    distributed. category_col is unused for a single-variable histogram —
    accepted only for a uniform renderer signature. Falls back to the original
    numeric-detection behavior otherwise."""
    if value_col and value_col in cols and _to_float(data[0].get(value_col)) is not None:
        col = value_col
    else:
        num = _numeric_cols(data, cols)
        col = num[0] if num else cols[0]
    vals = [_to_float(r[col]) for r in data if _to_float(r.get(col)) is not None]
    if not vals:
        return
    ax.hist(vals, bins=min(20, max(5, len(vals) // 5)), edgecolor="black", alpha=0.7)
    ax.set_xlabel(_humanize_column(col))
    ax.set_ylabel("Count")


def _chart_box(ax, data: list, cols: list, category_col: str = "", value_col: str = "") -> None:
    """category_col/value_col: see _chart_bar's docstring — same resolution/fallback
    precedence, applied within the original two-branch (grouped vs. single-variable)
    structure."""
    if len(cols) >= 2:
        cat_col = category_col if category_col and category_col in cols else cols[0]
        val_col = value_col if value_col and value_col in cols else cols[1]
        groups: dict = {}
        for row in data:
            g = str(row[cat_col])
            v = _to_float(row[val_col])
            if v is not None:
                groups.setdefault(g, []).append(v)
        if not groups:
            return
        group_labels = list(groups.keys())
        # tick_labels= is the current API (≥3.9); fall back for older versions.
        try:
            ax.boxplot([groups[g] for g in group_labels], tick_labels=group_labels, vert=True)
        except TypeError:
            ax.boxplot([groups[g] for g in group_labels], vert=True)
            ax.set_xticks(range(1, len(group_labels) + 1))
            ax.set_xticklabels(group_labels, rotation=45, ha="right", fontsize=8)
        ax.set_xlabel(_humanize_column(cat_col))
        ax.set_ylabel(_humanize_column(val_col))
    else:
        val_col = value_col if value_col and value_col in cols else cols[0]
        vals = [_to_float(r[val_col]) for r in data if _to_float(r.get(val_col)) is not None]
        if not vals:
            return
        ax.boxplot(vals)
        ax.set_ylabel(_humanize_column(val_col))


def _chart_stacked_bar(
    ax, data: list, cols: list,
    category_col: str = "", value_col: str = "", secondary_col: str = "",
) -> None:
    """A NULL metric value for a real (category, sub-category) cell is stacked
    as a 0-height segment (stacking needs a real number to sum), but that
    segment is hatched to mark it as "no data" — distinct from a real zero
    segment, which stacks the same way with no hatch.

    category_col = main (x-axis) category, secondary_col = sub-category (the
    stack dimension), value_col = the aggregate being stacked. Falls back to
    the original positional cols[0]/cols[1]/cols[2] behavior when not set."""
    import numpy as np
    if len(cols) < 3:
        _chart_bar(ax, data, cols, category_col=category_col, value_col=value_col)
        return
    cat_col = category_col if category_col and category_col in cols else cols[0]
    sub_col = secondary_col if secondary_col and secondary_col in cols else cols[1]
    val_col = value_col if value_col and value_col in cols else cols[2]
    pivot: dict = {}
    null_cells: set = set()
    subcat_order: list = []
    for row in data:
        cat = str(row[cat_col])
        sub = str(row[sub_col])
        val = _to_float(row[val_col])
        if val is None:
            null_cells.add((cat, sub))
            val = 0.0
        pivot.setdefault(cat, {})[sub] = val
        if sub not in subcat_order:
            subcat_order.append(sub)
    categories = list(pivot.keys())
    bottom = np.zeros(len(categories))
    for i, sc in enumerate(subcat_order):
        vals = [pivot[cat].get(sc, 0) for cat in categories]
        bars = ax.bar(categories, vals, bottom=bottom, label=sc, color=f"C{i % 10}")
        for j, cat in enumerate(categories):
            if (cat, sc) in null_cells:
                bars[j].set_hatch("//")
                bars[j].set_edgecolor("gray")
        bottom += np.array(vals)
    ax.set_xticklabels(categories, rotation=45, ha="right", fontsize=8)
    ax.set_xlabel(_humanize_column(cat_col))
    ax.set_ylabel(_humanize_column(val_col))
    ax.legend(title=_humanize_column(sub_col), bbox_to_anchor=(1.05, 1), loc="upper left", fontsize=8)


def _squarify_rects(sizes, x=0.0, y=0.0, width=1.0, height=1.0) -> list:
    """Minimal squarify layout — returns list of (x, y, w, h) tuples.

    Uses the squarify slice-and-dice algorithm so treemaps render without
    relying on the external squarify package (which uses deprecated
    matplotlib.cm.get_cmap in v0.4.4 and fails on matplotlib ≥3.9).
    """
    total = sum(sizes)
    if not sizes or total == 0:
        return []
    rects = []
    remaining = list(sizes)
    rx, ry, rw, rh = x, y, width, height
    while remaining:
        if rw >= rh:
            # Slice vertically
            slc_w = rw * remaining[0] / sum(remaining)
            col_sizes = []
            col_total = 0.0
            for s in remaining:
                if col_total + s <= sum(remaining) * slc_w / rw + 1e-9:
                    col_sizes.append(s)
                    col_total += s
                else:
                    break
            if not col_sizes:
                col_sizes = [remaining[0]]
            slc_w = rw * sum(col_sizes) / sum(remaining)
            cy = ry
            for s in col_sizes:
                ch = rh * s / sum(col_sizes)
                rects.append((rx, cy, slc_w, ch))
                cy += ch
            remaining = remaining[len(col_sizes):]
            rx += slc_w
            rw -= slc_w
        else:
            # Slice horizontally
            slc_h = rh * remaining[0] / sum(remaining)
            row_sizes = []
            row_total = 0.0
            for s in remaining:
                if row_total + s <= sum(remaining) * slc_h / rh + 1e-9:
                    row_sizes.append(s)
                    row_total += s
                else:
                    break
            if not row_sizes:
                row_sizes = [remaining[0]]
            slc_h = rh * sum(row_sizes) / sum(remaining)
            cx = rx
            for s in row_sizes:
                cw = rw * s / sum(row_sizes)
                rects.append((cx, ry, cw, slc_h))
                cx += cw
            remaining = remaining[len(row_sizes):]
            ry += slc_h
            rh -= slc_h
    return rects


def _chart_treemap(
    fig, data: list, cols: list,
    category_col: str = "", value_col: str = "", secondary_col: str = "",
) -> None:
    """A treemap rectangle's area IS its value — a NULL size has no valid area
    to draw at all (unlike a real zero, which legitimately occupies no area).
    Rows with a NULL size metric are omitted from the treemap entirely rather
    than silently plotted as a real (invisible) zero-area rectangle.

    category_col = outer/parent label, value_col = size metric. secondary_col,
    when present, is folded into the label as "outer / inner" so the nested
    dimension the question asked about is still visible without changing the
    rectangle layout itself. Falls back to the original cols[0]/cols[-1]
    behavior when not set."""
    ax = fig.add_subplot(111)
    if len(cols) < 2:
        return
    cat_col = category_col if category_col and category_col in cols else cols[0]
    val_col = value_col if value_col and value_col in cols else cols[-1]
    sub_col = secondary_col if secondary_col and secondary_col in cols else None
    if sub_col:
        pairs = [
            (f"{r[cat_col]} / {r[sub_col]}", _to_float(r[val_col])) for r in data
        ]
    else:
        pairs = [(str(r[cat_col]), _to_float(r[val_col])) for r in data]
    pairs = [(label, val) for label, val in pairs if val is not None]
    if not pairs:
        return
    labels = [label for label, _ in pairs]
    sizes = [abs(val) for _, val in pairs]
    if sum(sizes) == 0:
        return

    from matplotlib.patches import FancyBboxPatch

    rects = _squarify_rects(sizes)
    colors = [f"C{i % 10}" for i in range(len(labels))]
    for (x, y, w, h), label, color in zip(rects, labels, colors):
        patch = FancyBboxPatch(
            (x + 0.005, y + 0.005), max(w - 0.01, 0.001), max(h - 0.01, 0.001),
            linewidth=1, edgecolor="white", facecolor=color, alpha=0.85,
            boxstyle="round,pad=0",
        )
        ax.add_patch(patch)
        if w > 0.05 and h > 0.03:
            ax.text(
                x + w / 2, y + h / 2, label,
                ha="center", va="center", fontsize=min(8, max(5, int(w * 60))),
                wrap=True, clip_on=True,
            )
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.axis("off")
    ax.set_title(f"Treemap: {_humanize_column(val_col)}")


def _render_chart_image(
    data: list, chart_type: str, csv_path: Path,
    category_col: str = "", value_col: str = "", secondary_col: str = "",
) -> "Path | None":
    """Render a matplotlib chart from query result data and save as <csv_path>.png.

    category_col/value_col/secondary_col are the REAL result columns resolved
    by resolve_chart_columns (by meaning, not SQL SELECT-list position) — passed
    straight through to whichever _chart_* renderer is selected below. Each
    renderer falls back to its own original positional/numeric-detection logic
    when a given column isn't set or isn't actually present in this result's
    columns (see build_visualization for the precedence resolution).

    Returns the .png Path on success, None on failure (any exception is caught
    and logged to stderr so the caller can still return the CSV unaffected).
    Uses the Figure/FigureCanvasAgg API (no pyplot global state) for safe
    non-interactive server-side rendering.
    """
    if not data or not isinstance(data[0], dict):
        return None

    png_path = csv_path.with_suffix(".png")
    cols = list(data[0].keys())
    ct = chart_type.lower()

    try:
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        fig = Figure(figsize=(10, 6))
        FigureCanvasAgg(fig)

        if "stacked bar" in ct:
            ax = fig.add_subplot(111)
            _chart_stacked_bar(
                ax, data, cols,
                category_col=category_col, value_col=value_col, secondary_col=secondary_col,
            )
        elif "treemap" in ct:
            _chart_treemap(
                fig, data, cols,
                category_col=category_col, value_col=value_col, secondary_col=secondary_col,
            )
        elif "line" in ct:
            ax = fig.add_subplot(111)
            _chart_line(ax, data, cols, category_col=category_col, value_col=value_col)
        elif "bar" in ct:
            ax = fig.add_subplot(111)
            _chart_bar(ax, data, cols, category_col=category_col, value_col=value_col)
        elif "scatter" in ct:
            ax = fig.add_subplot(111)
            _chart_scatter(ax, data, cols, category_col=category_col, value_col=value_col)
        elif "donut" in ct:
            ax = fig.add_subplot(111)
            _chart_pie(ax, data, cols, donut=True, category_col=category_col, value_col=value_col)
        elif "pie" in ct:
            ax = fig.add_subplot(111)
            _chart_pie(ax, data, cols, donut=False, category_col=category_col, value_col=value_col)
        elif "histogram" in ct:
            ax = fig.add_subplot(111)
            _chart_histogram(ax, data, cols, category_col=category_col, value_col=value_col)
        elif "box" in ct:
            ax = fig.add_subplot(111)
            _chart_box(ax, data, cols, category_col=category_col, value_col=value_col)
        else:
            ax = fig.add_subplot(111)
            _chart_bar(ax, data, cols, category_col=category_col, value_col=value_col)

        fig.tight_layout()
        fig.savefig(str(png_path), dpi=150, bbox_inches="tight")
        return png_path

    except Exception as exc:
        import sys as _sys
        print(
            f"[chart render] failed for chart_type={chart_type!r}: {type(exc).__name__}: {exc}",
            file=_sys.stderr,
        )
        return None


BUILD_VISUALIZATION_SUMMARY_SYSTEM_PROMPT = """You are a data analyst writing a brief \
interpretive summary of a SQL query result for a chart.

Rules:
- Write exactly 2-3 sentences.
- Be specific: reference actual numbers, trends, or patterns visible in the data.
- Ground every claim strictly in the result shown — never invent a claim the data \
does not support.
- Do not describe the chart type or the SQL — only describe what the data shows."""


# ── Deterministic chart-type validation against the real result ────────────────
#
# determine_chart_type picks the chart type BEFORE generate_sql runs, purely
# from question wording — some of its own rules (e.g. "pie chart only for <=5
# categories") are unenforceable at that point since the real result doesn't
# exist yet. validate_chart_shape closes that gap: pure Python, no LLM call,
# checking the REAL parsed result against state.chart_type and overriding to a
# safer chart type when it would render broken or misleading. Only one level
# of fallback is ever applied — always lands on plain "bar" (or a stripped-down
# stacked-bar/treemap), never chains multiple substitutions.

_MIN_HISTOGRAM_BOX_ROWS = 10

_TEMPORAL_VALUE_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2}|\d{4}-\d{2}|\d{4}|"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*)\b",
    re.IGNORECASE,
)


def _canonical_chart_kind(chart_type: str) -> str:
    """Map a free-text chart_type string down to one of the canonical kinds this
    validation table has a rule for, using the same substring-matching approach
    as _render_chart_image/_chart_shaping_instruction. Anything that doesn't
    match a specific kind (including plain "bar") is treated as "bar", which has
    no constraint in the table below."""
    ct = (chart_type or "").lower()
    for key in ("stacked bar", "treemap", "line", "scatter", "donut", "pie", "histogram", "box"):
        if key in ct:
            return key
    return "bar"


def _looks_temporal(result_data: list, category_col: str) -> bool:
    """True when the category column's real values look like dates/years/months
    — a genuine time axis, not just an arbitrary category."""
    if not category_col:
        return False
    for row in result_data[:5]:
        v = row.get(category_col)
        if isinstance(v, (_dt.date, _dt.datetime)):
            return True
        if isinstance(v, str) and _TEMPORAL_VALUE_RE.match(v.strip()):
            return True
    return False


def _looks_naturally_ordered(result_data: list, category_col: str) -> bool:
    """True when the category column is numeric and monotonic across the
    result as returned (e.g. a sequential order number, an age) — a legitimate
    non-date x-axis for a line chart, distinct from an arbitrary category."""
    if not category_col or len(result_data) < 2:
        return False
    vals = [_to_float(r.get(category_col)) for r in result_data]
    if any(v is None for v in vals):
        return False
    return vals == sorted(vals) or vals == sorted(vals, reverse=True)


def validate_chart_shape(state: SQLAnalystState) -> dict:
    """Node (pure Python, no LLM call): validate state.chart_type against the
    REAL parsed result and override + record chart_type_override_note on any
    violation. Runs after resolve_chart_columns, before build_visualization.

    | chart_type          | constraint                                    | fallback |
    |----------------------|-----------------------------------------------|----------|
    | pie / donut          | <= 5 distinct category rows                   | bar      |
    | line                 | temporal category, or naturally ordered rows  | bar      |
    | scatter               | >= 2 real numeric cols (excl. id/count cols)  | bar      |
    | stacked bar / treemap | >= 3 columns present                          | bar      |
    | histogram / box      | >= ~10 rows returned                          | bar      |

    If the result is empty or unparseable, there's nothing to validate against —
    leave chart_type untouched (build_visualization's existing empty-result
    handling covers that case).
    """
    result_data, was_truncated = _parse_sql_result(state.sql_query_execution_result)
    if not result_data or not isinstance(result_data[0], dict):
        return {}

    cols = list(result_data[0].keys())
    category_col = (
        state.chart_category_column if state.chart_category_column in cols
        else (cols[0] if cols else "")
    )

    kind = _canonical_chart_kind(state.chart_type)
    n_rows = len(result_data)

    new_chart_type = None
    reason = ""

    if kind in ("pie", "donut"):
        distinct = {str(r.get(category_col)) for r in result_data}
        if len(distinct) > 5:
            new_chart_type = "bar"
            reason = (
                f"a {kind} chart is only readable with 5 or fewer categories, but this "
                f"result has {len(distinct)}"
            )
    elif kind == "line":
        if not (
            _looks_temporal(result_data, category_col)
            or _looks_naturally_ordered(result_data, category_col)
        ):
            new_chart_type = "bar"
            reason = (
                "the category column doesn't look like a time series or a naturally "
                "ordered sequence, so a line chart would draw a misleading trend"
            )
    elif kind == "scatter":
        numeric_cols = [
            c for c in cols
            if _to_float(result_data[0].get(c)) is not None
            and not _is_id_like_column(c)
            and not _COUNT_COL_RE.match(c)
        ]
        if len(numeric_cols) < 2:
            new_chart_type = "bar"
            reason = (
                "a scatter plot needs at least two real numeric measures (excluding "
                f"id/count columns), but this result only has {len(numeric_cols)}"
            )
    elif kind in ("stacked bar", "treemap"):
        if len(cols) < 3:
            new_chart_type = "bar"
            reason = (
                f"a {kind} chart needs at least 3 columns (category, sub-category, "
                f"value), but this result only has {len(cols)}"
            )
    elif kind in ("histogram", "box"):
        if n_rows < _MIN_HISTOGRAM_BOX_ROWS:
            new_chart_type = "bar"
            reason = (
                f"only {n_rows} rows were returned — too few to show a meaningful "
                f"{kind}"
            )

    if new_chart_type is None:
        return {}

    if state.chart_type_source == "explicit":
        # state.chart_type is user-authored text that often already contains the
        # word "chart" (e.g. "pie chart") — don't append a second one.
        asked_for = state.chart_type if "chart" in state.chart_type.lower() else f"{state.chart_type} chart"
        note = (
            f"You asked for a {asked_for}, but it was rendered as a "
            f"{new_chart_type} chart instead because {reason}."
        )
    else:
        note = (
            f"Rendered as a {new_chart_type} chart instead of the initially chosen "
            f"{state.chart_type} because {reason}."
        )

    return {
        "chart_type": new_chart_type,
        "chart_type_override_note": note,
    }


def build_visualization(state: SQLAnalystState) -> dict:
    """Node: write output file(s) from the SQL result and produce an interpretive summary.

    Replaces represent_final_answer specifically when wants_visualization=True.
    The normal represent_final_answer node is completely unchanged and still used
    for every non-visualization question.

    Steps:
    1. Parse the execution result (handles Decimal and datetime from psycopg2).
    2. Always write a CSV with human-readable headers (universal default output).
    3. When export_target == "tableau", also write a .hyper extract alongside the CSV.
    4. Ask the cheap LLM for a short, grounded interpretive summary.
    5. Compose final_answer: file(s) produced + chart type + reasoning (if reasoned) + summary.
    """
    # --- Parse result (handles Decimal / datetime from psycopg2 repr) ---
    # was_truncated is True when execute_sql capped the result at MAX_RESULT_ROWS —
    # the chart/CSV below is still built from the real (partial) rows returned, but
    # the final answer must say so explicitly rather than presenting a sample as if
    # it were the complete picture.
    result_data, was_truncated = _parse_sql_result(state.sql_query_execution_result)

    # --- Write CSV (always, regardless of export_target) ---
    viz_dir = _PROJECT_ROOT / "outputs" / "visualizations"
    viz_dir.mkdir(parents=True, exist_ok=True)

    question_slug = re.sub(r"[^a-z0-9]+", "_", state.curated_question.lower())[:40].strip("_")
    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{question_slug}_{timestamp}.csv"
    output_path = viz_dir / filename

    if result_data and isinstance(result_data, list) and isinstance(result_data[0], dict):
        raw_cols = list(result_data[0].keys())
        human_cols = [_humanize_column(c) for c in raw_cols]
        with open(output_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=human_cols)
            writer.writeheader()
            for row in result_data:
                writer.writerow(dict(zip(human_cols, row.values())))
    else:
        # Empty result or unexpected shape — write an empty CSV with a note.
        with open(output_path, "w", newline="") as f:
            f.write("(no data returned)\n")

    # --- Optionally write .hyper extract (Tableau only) ---
    hyper_path = None
    if state.export_target == "tableau":
        hyper_path = _write_hyper_file(result_data, output_path)

    # --- Resolve which real columns to pass to the renderer ---
    # Precedence: state.chart_category_column/chart_value_column/chart_secondary_column
    # (set by resolve_chart_columns) if set AND actually present in the real result's
    # columns; otherwise pass empty strings, in which case each _chart_* renderer
    # falls back to its own original positional/numeric-detection logic (not deleted,
    # just made secondary).
    render_cols = (
        list(result_data[0].keys()) if result_data and isinstance(result_data[0], dict) else []
    )
    render_category_col = state.chart_category_column if state.chart_category_column in render_cols else ""
    render_value_col = state.chart_value_column if state.chart_value_column in render_cols else ""
    render_secondary_col = state.chart_secondary_column if state.chart_secondary_column in render_cols else ""

    # --- Render chart image (always, regardless of export_target) ---
    # _render_chart_image catches all exceptions internally; a None return means
    # rendering failed and we report that honestly in final_answer.
    chart_png: "Path | None" = _render_chart_image(
        result_data, state.chart_type, output_path,
        category_col=render_category_col, value_col=render_value_col, secondary_col=render_secondary_col,
    )
    chart_image_path_str = str(chart_png) if chart_png is not None else ""

    # --- Interpretive summary ---
    llm = pick_llm("cheap")
    sample = result_data[:10] if isinstance(result_data, list) else []
    summary_human = (
        f"Chart type: {state.chart_type}\n"
        f"User question: {state.user_question}\n"
        f"SQL result (up to 10 rows): {sample}"
    )
    summary_response = llm.invoke(
        [
            ("system", BUILD_VISUALIZATION_SUMMARY_SYSTEM_PROMPT),
            ("human", summary_human),
        ]
    )
    summary = _extract_text(summary_response.content).strip()

    # --- Compose final answer ---
    reasoning_note = ""
    if state.chart_type_source == "reasoned" and state.chart_type_reasoning:
        reasoning_note = f"\nChart type reasoning: {state.chart_type_reasoning}"

    truncation_note = ""
    if was_truncated:
        truncation_note = (
            f"\n\nNote: this query matched more rows than could be retrieved "
            f"(results are capped at {MAX_RESULT_ROWS} rows) — the chart and CSV "
            "below are built from only that partial sample, not the full result set."
        )

    if hyper_path is not None:
        files_note = (
            f"Files produced:\n"
            f"  CSV:    {output_path}\n"
            f"  Hyper:  {hyper_path}"
        )
    else:
        files_note = f"Visualization data saved to: {output_path}"

    if chart_png is not None:
        files_note += f"\n  Chart:  {chart_png}"
        chart_render_note = ""
    else:
        chart_render_note = "\nNote: Chart image rendering failed — see stderr for details."

    # Deterministic, mandatory transparency (same rationale as represent_final_answer):
    # extracted mechanically from the real executed SQL and parsed result data.
    disclosure = _analyst_judgment_disclosure(state.generated_sql_query, result_data)
    disclosure_note = f"\n\nHow this answer was computed: {disclosure}" if disclosure else ""

    # Never swallow these silently — both are set only when something needed
    # correcting (a low-confidence column pick, or a chart type that would have
    # rendered broken/misleading against the real result).
    column_resolution_note = (
        f"\n\nNote: {state.chart_column_resolution_note}"
        if state.chart_column_resolution_note else ""
    )
    chart_override_note = (
        f"\n\nNote: {state.chart_type_override_note}"
        if state.chart_type_override_note else ""
    )

    final_answer = (
        f"{files_note}\n"
        f"Chart type: {state.chart_type}{reasoning_note}{chart_render_note}{truncation_note}\n\n"
        f"Summary: {summary}{disclosure_note}{column_resolution_note}{chart_override_note}"
    )
    final_answer = _apply_causal_correction(final_answer, state.generated_sql_query)

    return {
        "output_file_path": str(output_path),
        "chart_image_path": chart_image_path_str,
        "final_answer": final_answer,
        "messages": [AIMessage(content=final_answer)],
    }


def route_after_add_context(state: SQLAnalystState) -> str:
    """Conditional edge after add_context.

    Priority order:
    1. "needs_cleaning" — at least one fail-level table with a known source_folder
       not yet attempted this question: fire clean_and_reload first.
    2. "determine_chart_type" — visualization request (wants_visualization=True)
       with a clean data quality check: determine chart type before generating SQL.
    3. "generate_sql" — normal question, proceed directly.
    """
    if state.data_quality_action == "needs_cleaning":
        return "needs_cleaning"
    if state.wants_visualization:
        return "determine_chart_type"
    return "generate_sql"


def _find_source_csv(folder_path, table_name: str, sanitize_identifier):
    """Reverse-map table_name back to its raw CSV file in folder_path by sanitized
    stem, or None if no CSV in the folder matches. sanitize_identifier is passed in
    rather than imported here so callers can keep their existing local import."""
    for csv_path in sorted(folder_path.glob("*.csv")):
        if sanitize_identifier(csv_path.stem) == table_name:
            return csv_path
    return None


def clean_and_reload(state: SQLAnalystState, _llm=None) -> dict:
    """Node: for each fail-level table that has a known source_folder, run
    clean_dataset() against that folder, then reload the table and update its
    _data_quality_status row with the new outcome.

    Fires AT MOST ONCE per table per question — cleaning_attempted_tables (returned
    here and checked by add_context on the next pass) enforces the stop condition.

    After this node, the graph unconditionally routes back to add_context so schema
    and status context refresh; add_context's updated data_quality_action then
    determines whether to proceed to generate_sql (either the table improved, or it
    was already attempted and can't be retried).

    _llm is a test-only injection point: when None (always in production) clean_dataset
    uses its own real LLM. Tests pass a deterministic fake to keep cleaning predictable.

    IMPORTANT — reload coverage semantics (architecture review point #21): clean_dataset()
    below is called ONCE per source_folder and processes EVERY CSV in that folder, not
    just the ones that triggered this redirect. But only the tables actually listed in
    state.tables_to_clean (the ones with an originally-flagged fail-level status row)
    get reloaded into Postgres afterward, in the per-table loop below. A folder can
    contain other CSVs that also get cleaned/cloned as a side effect of that one
    clean_dataset() call (e.g. files with only warn-level issues, or files that were
    already passing but still had something cosmetic flagged) — those cleaned outputs
    land in cleaned/ like any other, but are deliberately NOT reloaded here, to avoid
    redundant reload work for tables that were never flagged as needing it in the
    first place. Do not assume every file clean_dataset() touches gets a fresh table
    in the database — only the tables in state.tables_to_clean do.

    FAILS CLOSED in a non-interactive context (architecture review point #24): this
    node is reachable automatically from a plain, read-only question via
    add_context's auto-clean redirect — not something the user explicitly asked to
    trigger. clean_dataset() below can reach _request_approval's real input() call
    for any file with real issues, anywhere in the target folder. If this graph is
    ever invoked from a context with no attached interactive terminal (a Telegram
    gateway, a future API), that input() call would either block forever on a stdin
    that will never receive anything, or crash with EOFError. So BEFORE doing
    anything else — before even opening a DB connection — this checks
    sys.stdin.isatty() and, if false, returns a clear final_answer explaining that
    this table needs cleaning approval that can't be obtained in this context,
    without ever calling clean_dataset()/input(). Auto-clean-triggered approval
    requires an interactive session; a fully autonomous/headless cleaning-approval
    flow is out of scope for now and would need to be designed separately. Manual
    CLI cleaning (clean_data.py, utils/load_data.py) and this project's test suite
    are unaffected — they call clean_dataset()/_request_approval directly, not
    through this node, and deliberately pipe answers to a non-tty stdin that
    input() reads from successfully.
    """
    import sys as _sys
    from pathlib import Path

    from utils.data_cleaning import _read_csv_robust, clean_dataset, unresolved_issues_for_record
    from utils.load_data import (
        check_source_freshness,
        compute_and_write_fanout_status,
        compute_file_checksum,
        compute_quality_status,
        ensure_cleaning_recipes_table,
        ensure_data_quality_status_table,
        ensure_derived_columns_table,
        ensure_fanout_status_table,
        ensure_transformation_candidates_table,
        ensure_transformation_decisions_table,
        get_admin_connection,
        invalidate_cached_decisions_for_table,
        invalidate_cleaning_recipes_for_table,
        load_csv_to_table,
        sanitize_identifier,
        write_data_quality_status,
        write_transformation_candidates,
    )
    from utils.transformation_options import detect_transformation_candidates

    if not _sys.stdin.isatty():
        table_names_all = [item["table"] for item in state.tables_to_clean]
        return {
            "final_answer": (
                f"I can't answer this because table(s) {', '.join(table_names_all)} "
                f"need data-cleaning approval before they can be reloaded, and this "
                f"session has no interactive terminal to ask for that approval. "
                f"Auto-clean-triggered cleaning requires an interactive session — "
                f"please run this from one so the cleaning steps can be reviewed "
                f"and approved."
            ),
        }

    newly_attempted = list(state.cleaning_attempted_tables)

    # Group tables by source_folder: one clean_dataset() call per folder covers all
    # CSVs in it, so multiple fail tables from the same dataset need only one run.
    folder_to_tables: dict = {}
    for item in state.tables_to_clean:
        sf = item["source_folder"]
        folder_to_tables.setdefault(sf, []).append(item["table"])

    conn = get_admin_connection()
    try:
        ensure_data_quality_status_table(conn)
        ensure_fanout_status_table(conn)
        ensure_transformation_candidates_table(conn)
        ensure_transformation_decisions_table(conn)
        ensure_derived_columns_table(conn)
        ensure_cleaning_recipes_table(conn)

        for source_folder, table_names in folder_to_tables.items():
            folder_path = Path(source_folder)

            # Source-checksum freshness check (architecture review point #20): before
            # running clean_dataset for this folder, compare each targeted table's
            # CURRENT raw source file against the checksum recorded the last time it
            # was processed. clean_dataset() below always re-examines the file's
            # real, current bytes regardless of this check's outcome — this is a
            # deliberate detection/audit step, not a gate, so a maintainer never
            # mistakes what's about to happen for a stale, reused result when the
            # source has actually changed underneath the table since it was last
            # cleaned.
            for table_name in table_names:
                target_csv = _find_source_csv(folder_path, table_name, sanitize_identifier)
                if target_csv is None:
                    continue
                changed, _current_checksum = check_source_freshness(conn, table_name, target_csv)
                if changed:
                    print(
                        f"[checksum] source changed, forcing fresh clean for "
                        f"'{table_name}' ({target_csv}).",
                        file=_sys.stderr,
                    )

            cleaning_result = clean_dataset(folder_path, llm=_llm, trigger="auto_redirect", recipe_conn=conn)

            all_records = {rec.file_name: rec for rec in cleaning_result.cleaned_files}
            all_records.update({rec.file_name: rec for rec in cleaning_result.skipped_files})

            for table_name in table_names:
                target_csv = _find_source_csv(folder_path, table_name, sanitize_identifier)

                if target_csv is None:
                    # CSV not found in folder — mark attempted, skip reload.
                    newly_attempted.append(table_name)
                    continue

                csv_name = target_csv.name
                if csv_name in all_records:
                    load_path = Path(cleaning_result.cleaned_dir) / csv_name
                else:
                    load_path = target_csv

                load_csv_to_table(conn, load_path)

                rec = all_records.get(csv_name)
                if rec is None:
                    unresolved = []
                    was_cleaned = False
                else:
                    unresolved = unresolved_issues_for_record(rec)
                    was_cleaned = True

                status, issues_found = compute_quality_status(unresolved)
                # Checksum is always computed from the RAW source file (target_csv),
                # never the cleaned/ clone — it tracks whether the source has
                # changed, not whether cleaning changed its output.
                write_data_quality_status(
                    conn, table_name, status, issues_found, was_cleaned,
                    source_folder=source_folder,
                    source_checksum=compute_file_checksum(target_csv),
                )
                compute_and_write_fanout_status(conn, table_name)

                # Spec 1, Part 0: (re-)detect Transformation Options candidates on
                # this fresh reload and invalidate any previously-cached decisions
                # for this table — see the reload-invalidation rule in
                # utils/transformation_options.py's module docstring.
                reloaded_df = _read_csv_robust(load_path)
                candidates = detect_transformation_candidates(reloaded_df, table_name)
                write_transformation_candidates(conn, table_name, candidates)
                invalidate_cached_decisions_for_table(conn, table_name)
                invalidate_cleaning_recipes_for_table(conn, table_name)

                newly_attempted.append(table_name)
    finally:
        conn.close()

    return {"cleaning_attempted_tables": newly_attempted}


def route_after_clean_and_reload(state: SQLAnalystState) -> str:
    """Conditional edge after clean_and_reload.

    Normally loops back to add_context to refresh schema/status context (the usual
    case). But if clean_and_reload had to fail closed — e.g. the non-interactive
    approval gate (architecture review point #24) — it sets final_answer directly and
    this routes straight to END instead of looping back, since there is nothing left
    for add_context/generate_sql to usefully do with a question that can't proceed.
    """
    if state.final_answer:
        return "end"
    return "add_context"


_TABLE_CONTEXT_RE = re.compile(r"^Table: (\S+)$", re.MULTILINE)


def _transformation_menu_for(candidate) -> tuple:
    """Builds the (context, options) pair for present_transformation_options
    for one candidate. Every kind gets a plain "Apply now" option except
    "company_age", which gets Part 7's own explicit missing-Founded
    sub-choice — the "age 0" option is labeled as a deliberate assumption,
    never presented as an observed fact."""
    context = {
        "title": f"{candidate.kind.replace('_', ' ').title()} for {', '.join(candidate.columns)}?",
        "what_was_found": candidate.description,
        "why_optional": (
            "This is a judgment call (schema enrichment or style choice), not a "
            "correctness fix — reasonable people could choose differently, or skip "
            "it entirely."
        ),
    }
    if candidate.kind == "company_age":
        options = [
            {
                "id": "apply_null",
                "label": "Apply (missing founding year -> null age)",
                "description": "Rows with no usable founding year get a null company_age.",
            },
            {
                "id": "apply_age_zero",
                "label": "Apply (missing founding year -> age 0 — DELIBERATE ASSUMPTION)",
                "description": (
                    "Rows with no usable founding year are ASSUMED founded this year — "
                    "this is a deliberate assumption, not an observed fact."
                ),
            },
        ]
    else:
        options = [
            {"id": "apply", "label": "Apply now", "description": "Add/apply this transformation now."}
        ]
    return context, options


def _apply_chosen_transformation(
    conn, table_name: str, source_folder: str, candidate, chosen_option_id: str,
    manual_mapping: "dict | None" = None,
) -> bool:
    """Actually runs the chosen Transformation Options fix (Spec 1, Parts 6/7/8;
    Spec 3, Part 2's categorical_consolidation) against the table's real
    cleaned CSV, reloads the table, marks any new columns "derived, not
    source" (Part 7), and refreshes the stored candidate list against the
    now-current file — mirrors clean_and_reload's own mutate -> reload ->
    refresh-bookkeeping sequence, but for an opt-in enrichment/style
    decision instead of a mandatory correctness fix.

    manual_mapping (Spec 3, Part 1): when given for a categorical_
    consolidation candidate, IS the raw_value->group mapping to apply
    directly — the live AI-clustering call is skipped entirely (Part 2's own
    "manual-mode behavior" requirement). None (the normal, non-manual path)
    means generate the mapping live via _generate_categorical_consolidation_mapping.

    Deliberately does NOT call invalidate_cached_decisions_for_table here —
    that's reserved for a genuine source-file reload/re-clean (see
    utils/transformation_options.py's module docstring); the decision just
    made for THIS candidate must survive this same reload, not be wiped by it.

    Returns True if something was actually applied (schema may have
    changed), False for "skip" or a decline/failure outcome.
    """
    if chosen_option_id == "skip":
        return False

    from utils.data_cleaning import (
        _detect_label_simplification_columns,
        _detect_range_columns,
        _read_csv_robust,
        decompose_range_column,
        simplify_labels,
    )
    from utils.feature_derivation import derive_features
    from utils.load_data import (
        load_csv_to_table,
        mark_derived_columns,
        sanitize_identifier,
        write_transformation_candidates,
    )
    from utils.transformation_options import detect_transformation_candidates

    target_csv = _find_source_csv(Path(source_folder), table_name, sanitize_identifier)
    if target_csv is None:
        return False
    load_path = target_csv.parent / "cleaned" / target_csv.name
    if not load_path.exists():
        load_path = target_csv

    column = candidate.columns[0]
    df = _read_csv_robust(load_path)
    mark_kind = None
    new_columns: list = []

    if candidate.kind == "range_decomposition":
        entry = next((e for e in _detect_range_columns(df) if e["column"] == column), None)
        if entry is None:
            return False
        result = decompose_range_column(load_path, entry)
        new_columns = result.get("new_columns", [])
        mark_kind = "range_decomposition"
    elif candidate.kind == "label_simplification":
        entry = next((e for e in _detect_label_simplification_columns(df) if e["column"] == column), None)
        if entry is None:
            return False
        result = simplify_labels(load_path, entry)
    elif candidate.kind == "categorical_consolidation":
        from utils.categorical_consolidation import (
            _generate_categorical_consolidation_mapping,
            consolidate_categorical_column,
        )

        if manual_mapping:
            mapping = manual_mapping
        else:
            distinct_values = df[column].dropna().unique().tolist()
            mapping = _generate_categorical_consolidation_mapping(distinct_values, pick_llm("cheap"))
        if not mapping:
            return False
        result = consolidate_categorical_column(load_path, candidate, mapping)
        if result.get("status") == "resolved":
            new_columns = [result["new_column"]]
        mark_kind = "categorical_consolidation"
    else:
        feature_spec = {"kind": candidate.kind, "columns": candidate.columns}
        if candidate.kind == "company_age":
            feature_spec["missing_founded_choice"] = (
                "age_zero" if chosen_option_id == "apply_age_zero" else "null"
            )
        result = derive_features(load_path, feature_spec)
        new_columns = result.get("new_columns", [])
        mark_kind = candidate.kind

    if result.get("status") != "resolved":
        return False

    load_csv_to_table(conn, load_path)
    if new_columns and mark_kind:
        mark_derived_columns(conn, table_name, new_columns, mark_kind)

    refreshed_df = _read_csv_robust(load_path)
    write_transformation_candidates(
        conn, table_name, detect_transformation_candidates(refreshed_df, table_name)
    )
    return True


def _distinct_values_for_candidate(source_folder: str, table_name: str, candidate) -> list:
    """Loads the real distinct values of a candidate's flagged column from
    its cleaned CSV — needed by manual mode (Spec 3) to check override
    completeness / drive resolve_manual_mode_candidate for
    categorical_consolidation, without requiring surface_transformations to
    always load the CSV for every candidate kind."""
    from utils.data_cleaning import _read_csv_robust
    from utils.load_data import sanitize_identifier

    target_csv = _find_source_csv(Path(source_folder), table_name, sanitize_identifier)
    if target_csv is None:
        return []
    load_path = target_csv.parent / "cleaned" / target_csv.name
    if not load_path.exists():
        load_path = target_csv
    df = _read_csv_robust(load_path)
    column = candidate.columns[0]
    if column not in df.columns:
        return []
    return df[column].dropna().unique().tolist()


def surface_transformations(state: SQLAnalystState) -> dict:
    """Node (Spec 1, Parts 0.5/1 — live wiring): runs right after add_context,
    before generate_sql/determine_chart_type.

    For every real table currently in prompt_query_context (parsed from its
    own "Table: <name>" lines — no extra schema query needed), reads that
    table's STORED Transformation Options candidates (read_transformation_candidates
    — never re-detects live, per Part 0's own discipline) and filters them to
    THIS question via surface_relevant_transformations. A table still needing
    Phase 1 cleaning (status "fail", or no known source_folder) is skipped —
    route_after_add_context already sends those to clean_and_reload first;
    Transformation Options only ever applies to an already-clean table.

    Any relevant candidate not already decided (read_transformation_decision
    against the durable _transformation_decisions cache) is presented via
    present_transformation_options; if "apply" is chosen, the fix is applied
    and the table reloaded immediately (_apply_chosen_transformation) so a
    newly-derived column can be available to THIS SAME question's SQL
    generation, not just the next one.

    chart_category_column/chart_value_column are not yet known at this point
    in the graph (resolved later, after execute_sql, on the visualization
    path only) — surfacing here relies on surface_relevant_transformations'
    other two signals instead: a touched-column match against
    curated_question's own text, and a relevance_tags match against
    curated_question directly.

    Deliberately does NOT fail closed like clean_and_reload: Transformation
    Options is optional enrichment, never required to answer a question, so a
    non-interactive session (sys.stdin.isatty() is False) simply SKIPS
    surfacing entirely (logged to stderr) rather than blocking or aborting —
    a real, deliberate difference from clean_and_reload's mandatory,
    fail-closed remediation gate.

    If anything was actually applied, re-runs add_context itself to refresh
    prompt_query_context/data_quality_warnings/data_quality_action/
    tables_to_clean before generate_sql/route_after_add_context ever see
    them; the accumulated narrative-log fields (see below) are always merged
    into whatever is returned, applied or not.

    Narrative logging (Spec 2, Part B): every candidate this call actually
    surfaces (via surface_relevant_transformations) and its real decision —
    freshly asked this call, or pulled from the _transformation_decisions
    cache — is appended to state.transformation_narrative_log, and every
    candidate that EXISTS for a touched table but was NOT surfaced is
    appended to state.transformation_candidates_not_relevant. Both lists
    accumulate across repeat calls within the same run (e.g. after a
    clean_and_reload loop) rather than being overwritten, since this node has
    no LangGraph reducer for them. utils/narrative.py's
    build_narrative_walkthrough reads these two fields straight off the
    logged query_log.jsonl entry — nothing here is re-derived downstream.

    Manual mode (Spec 3, Part 1): for each relevant candidate not already
    cached, checks utils.manual_mode.get_manual_mode_override before ever
    reaching present_transformation_options. A registered override (even an
    incomplete one) resolves it via resolve_manual_mode_candidate (Part 5's
    progressive disclosure — asks only about genuine gaps) instead of
    blocking on the normal live menu; the resolved decision is logged to
    _transformation_decisions with reasoning_shown={"source": "manual_mode",
    ...} exactly like Part 1 specifies, and applied through the same
    _apply_chosen_transformation path (with manual_mapping threaded through
    for categorical_consolidation, skipping the live AI-clustering call).
    Choosing option [3]/[4] in that interactive flow returns None, which
    falls through to the ordinary live present_transformation_options menu
    below — manual mode never silently blocks a candidate it declines to
    resolve.
    """
    import sys as _sys

    from utils.load_data import (
        get_admin_connection,
        read_transformation_candidates,
        read_transformation_decision,
        write_transformation_decision,
    )
    from utils.manual_mode import _is_override_complete, get_manual_mode_override, resolve_manual_mode_candidate
    from utils.transformation_options import present_transformation_options, surface_relevant_transformations

    narrative_log = list(state.transformation_narrative_log)
    not_relevant_log = list(state.transformation_candidates_not_relevant)

    if not _sys.stdin.isatty():
        print(
            "[transformation-options] no interactive terminal available — skipping "
            "Transformation Options surfacing for this question.",
            file=_sys.stderr,
        )
        return {}

    table_names = _TABLE_CONTEXT_RE.findall(state.prompt_query_context or "")
    if not table_names:
        return {}

    conn = get_admin_connection()
    applied_anything = False
    try:
        status_by_table = _fetch_data_quality_status(conn, table_names)
        for table_name in table_names:
            entry = status_by_table.get(table_name)
            if entry is None:
                continue
            status, _issues, source_folder = entry
            if status == "fail" or source_folder is None:
                continue

            candidates = read_transformation_candidates(conn, table_name)
            if not candidates:
                continue

            relevant = surface_relevant_transformations(
                curated_question=state.curated_question,
                chart_category_column="",
                chart_value_column="",
                touched_columns=[],
                candidates=candidates,
            )
            relevant_ids = {c.candidate_id for c in relevant}
            for candidate in candidates:
                if candidate.candidate_id not in relevant_ids:
                    not_relevant_log.append(
                        {"table_name": table_name, "candidate": candidate.to_dict()}
                    )

            for candidate in relevant:
                cached = read_transformation_decision(conn, table_name, candidate.candidate_id)
                if cached is not None:
                    # Already decided — reused silently, never re-asked (Part 1).
                    narrative_log.append(
                        {
                            "table_name": table_name,
                            "candidate": candidate.to_dict(),
                            "chosen_option_id": cached["chosen_option_id"],
                            "reasoning_shown": cached["reasoning_shown"],
                            "fresh": False,
                            "reload_reask": False,
                            "decided_at": cached.get("decided_at"),
                        }
                    )
                    continue

                # Spec 3, Part 1: manual mode. A registered override for this
                # candidate_id (even if incomplete) means "under manual mode"
                # — resolve it (progressively filling gaps, Part 5) instead
                # of falling straight to the live present_transformation_options
                # menu below.
                override = get_manual_mode_override(candidate.candidate_id)
                if override is not None:
                    distinct_values = None
                    if candidate.kind == "categorical_consolidation":
                        distinct_values = _distinct_values_for_candidate(source_folder, table_name, candidate)
                    if not _is_override_complete(candidate, override, distinct_values):
                        override = resolve_manual_mode_candidate(
                            candidate, partial_override=override, distinct_values=distinct_values,
                            llm=pick_llm("cheap"),
                        )
                        # None means the operator chose option [3]/[4] — an
                        # explicit, one-time opt-out for THIS candidate this
                        # run; falls through to the normal live menu below,
                        # exactly like manual mode was never active for it.
                    if override is not None:
                        decision = {
                            "chosen_option_id": override.chosen_option_id,
                            "reasoning_shown": {
                                "source": "manual_mode",
                                "reference": override.reference_source,
                                "supplied_data": override.supplied_data,
                            },
                        }
                        write_transformation_decision(conn, table_name, candidate.candidate_id, decision)
                        narrative_log.append(
                            {
                                "table_name": table_name,
                                "candidate": candidate.to_dict(),
                                "chosen_option_id": decision["chosen_option_id"],
                                "reasoning_shown": decision["reasoning_shown"],
                                "fresh": True,
                                "reload_reask": False,
                                "decided_at": None,
                            }
                        )
                        manual_mapping = (
                            override.supplied_data if candidate.kind == "categorical_consolidation" else None
                        )
                        if _apply_chosen_transformation(
                            conn, table_name, source_folder, candidate, decision["chosen_option_id"],
                            manual_mapping=manual_mapping,
                        ):
                            applied_anything = True
                        continue

                context, options = _transformation_menu_for(candidate)
                decision = present_transformation_options(
                    table_name=table_name,
                    candidate_id=candidate.candidate_id,
                    context=context,
                    options=options,
                    conn=conn,
                )
                # A "reload re-ask" is only ever claimed when it's actually
                # knowable within THIS run — never inferred from cross-session
                # log archaeology (Spec 2's own anti-fabrication rule): this
                # table must have genuinely gone through clean_and_reload
                # earlier in this same run.
                reload_reask = table_name in state.cleaning_attempted_tables
                narrative_log.append(
                    {
                        "table_name": table_name,
                        "candidate": candidate.to_dict(),
                        "chosen_option_id": decision["chosen_option_id"],
                        "reasoning_shown": decision["reasoning_shown"],
                        "fresh": True,
                        "reload_reask": reload_reask,
                        "decided_at": None,
                    }
                )
                if _apply_chosen_transformation(
                    conn, table_name, source_folder, candidate, decision["chosen_option_id"]
                ):
                    applied_anything = True
    finally:
        conn.close()

    narrative_fields = {
        "transformation_narrative_log": narrative_log,
        "transformation_candidates_not_relevant": not_relevant_log,
    }
    if not applied_anything:
        return narrative_fields
    return {**add_context(state), **narrative_fields}


def build_sql_analyst_graph():
    """Wire all nodes into a StateGraph using SQLAnalystState, and compile it.

    Graph shape (normal ask: path, wants_visualization=False):
        START -> curate_question -> add_context -> surface_transformations
        surface_transformations --(route_after_add_context)--> generate_sql | clean_and_reload
            (Spec 1, Parts 0.5/1: surfaces/applies any relevant Transformation
            Options candidate for an already-clean table before generate_sql
            ever runs — see surface_transformations' own docstring. A plain
            pass-through, {} state update, when nothing was applied.)
        clean_and_reload --(route_after_clean_and_reload)--> add_context (loop, via
            surface_transformations again; stop condition via
            cleaning_attempted_tables) | END (fail-closed: no interactive
            stdin available to grant cleaning approval, see ApprovalUnavailableError)
        generate_sql -> is_safe
        is_safe --(route_after_safety_check)--> execute_sql | cancel_sql
        execute_sql --(route_after_execute_sql)--> generate_sql (retry) | represent_final_answer
        cancel_sql -> END
        represent_final_answer -> END

    Additional visualization path (wants_visualization=True):
        surface_transformations --(route_after_add_context)--> determine_chart_type
        determine_chart_type -> generate_sql  (same generate_sql, extended prompt)
        execute_sql --(route_after_execute_sql)--> resolve_chart_columns
        resolve_chart_columns -> validate_chart_shape -> build_visualization
        build_visualization -> END
    """
    graph = StateGraph(SQLAnalystState)

    graph.add_node("curate_question", curate_question)
    graph.add_node("add_context", add_context)
    graph.add_node("surface_transformations", surface_transformations)
    graph.add_node("clean_and_reload", clean_and_reload)
    graph.add_node("determine_chart_type", determine_chart_type)
    graph.add_node("generate_sql", generate_sql)
    graph.add_node("is_safe", is_safe)
    graph.add_node("execute_sql", execute_sql)
    graph.add_node("cancel_sql", cancel_sql)
    graph.add_node("represent_final_answer", represent_final_answer)
    graph.add_node("resolve_chart_columns", resolve_chart_columns)
    graph.add_node("validate_chart_shape", validate_chart_shape)
    graph.add_node("build_visualization", build_visualization)

    graph.add_edge(START, "curate_question")
    graph.add_edge("curate_question", "add_context")
    graph.add_edge("add_context", "surface_transformations")
    graph.add_conditional_edges(
        "surface_transformations",
        route_after_add_context,
        {
            "generate_sql": "generate_sql",
            "determine_chart_type": "determine_chart_type",
            "needs_cleaning": "clean_and_reload",
        },
    )
    graph.add_conditional_edges(
        "clean_and_reload",
        route_after_clean_and_reload,
        {"add_context": "add_context", "end": END},
    )
    graph.add_edge("determine_chart_type", "generate_sql")
    graph.add_edge("generate_sql", "is_safe")

    graph.add_conditional_edges(
        "is_safe",
        route_after_safety_check,
        {"execute_sql": "execute_sql", "cancel_sql": "cancel_sql"},
    )
    graph.add_conditional_edges(
        "execute_sql",
        route_after_execute_sql,
        {
            "generate_sql": "generate_sql",
            "represent_final_answer": "represent_final_answer",
            "resolve_chart_columns": "resolve_chart_columns",
        },
    )
    graph.add_edge("resolve_chart_columns", "validate_chart_shape")
    graph.add_edge("validate_chart_shape", "build_visualization")

    graph.add_edge("cancel_sql", END)
    graph.add_edge("represent_final_answer", END)
    graph.add_edge("build_visualization", END)

    return graph.compile()
