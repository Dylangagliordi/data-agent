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

import sqlglot
from sqlglot import exp
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

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
    if warn_only_for is not None:
        tables = {t: cols for t, cols in tables.items() if t in warn_only_for}

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

    warnings = []
    for table, columns in tables.items():
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
                  AND table_name NOT IN (%s, %s)
                ORDER BY table_name, ordinal_position
                """,
                ("public", "_data_quality_status", "_fanout_status"),
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
                if source_folder is not None and table_name not in already_attempted:
                    tables_to_clean.append({"table": table_name, "source_folder": source_folder})
            # status == "warn" or "pass": nothing injected into generate_sql's context.

        sections = []
        with conn.cursor() as cur:
            for table_name, columns in tables.items():
                col_lines = "\n".join(f"  - {c} ({t})" for c, t in columns)

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
determine the most appropriate chart type.

Step 1: Check whether the question explicitly names a chart type.
Examples of explicit naming: "bar chart", "line graph", "pie chart", "scatter plot", "histogram", \
"box plot", "donut chart", "stacked bar", "treemap".
If a chart type is explicitly named, use it exactly as the chart_type. Set chart_type_source to \
"explicit" and chart_type_reasoning to an empty string — no justification is needed for something \
the user already specified.

Step 2: If no chart type is named, choose the best fit using this rubric:
- Change over time / trend → line chart
- Comparing categories (not over time) → bar chart
- Relationship between two numeric variables → scatter plot
- Proportion of a whole (5 or fewer categories ONLY) → pie chart (never for comparisons or distributions)
- Distribution of one variable → histogram
- Distribution of one variable across groups → box plot
- Composition across several categories → stacked bar chart
- Hierarchical part-to-whole → treemap
If genuinely uncertain between two reasonable fits, default to whichever of line/bar/scatter is \
closest, and say so explicitly in the reasoning.
Set chart_type_source to "reasoned". chart_type_reasoning must be a real, specific justification \
grounded in the question — never a generic placeholder like "best fit" or "seems appropriate"."""


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
            "metrics (avg per entity) over raw totals when groups have different sizes."
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

    return {"generated_sql_query": sql_query}


IS_SAFE_SYSTEM_PROMPT = """You are a security judge reviewing a SQL query. Your ONLY job is \
to decide whether this query is strictly read-only.

Answer "no" if the query contains, anywhere in it, any of: INSERT, UPDATE, DELETE, DROP, \
ALTER, TRUNCATE (in any case, including inside subqueries, CTEs, or comments). Otherwise \
answer "yes".

Do not evaluate correctness, style, or performance — only read-only safety. Give a brief \
reason in comments."""


def is_safe(state: SQLAnalystState) -> dict:
    """Node 4 (medium tier, JudgeSchema via with_structured_output).

    Receives ONLY the generated SQL text — never the original or curated question.
    """
    llm = pick_llm("cheap").with_structured_output(JudgeSchema)
    judgement: JudgeSchema = llm.invoke(
        [
            ("system", IS_SAFE_SYSTEM_PROMPT),
            ("human", state.generated_sql_query),
        ]
    )

    return {"is_safe": judgement.answer, "comments": judgement.comments}


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
    retryable error, "build_visualization" on success when wants_visualization
    is True, and "represent_final_answer" on success for a normal question.
    """
    if state.final_answer:
        # execute_sql already gave up after MAX_SQL_ATTEMPTS — pass the error
        # through represent_final_answer unchanged, regardless of wants_visualization.
        return "represent_final_answer"
    if state.sql_query_execution_result.startswith(_SQL_ERROR_PREFIX):
        return "generate_sql"
    if state.wants_visualization:
        return "build_visualization"
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


def _chart_bar(ax, data: list, cols: list) -> None:
    x_labels = [str(r[cols[0]]) for r in data]
    y_vals = [_to_float(r[cols[1]]) or 0 for r in data] if len(cols) >= 2 else list(range(len(data)))
    pos = list(range(len(x_labels)))
    ax.bar(pos, y_vals)
    ax.set_xticks(pos)
    ax.set_xticklabels(x_labels, rotation=45, ha="right", fontsize=8)
    ax.set_xlabel(_humanize_column(cols[0]))
    if len(cols) >= 2:
        ax.set_ylabel(_humanize_column(cols[1]))


def _chart_line(ax, data: list, cols: list) -> None:
    x_labels = [str(r[cols[0]]) for r in data]
    y_vals = [_to_float(r[cols[1]]) or 0 for r in data] if len(cols) >= 2 else []
    pos = list(range(len(x_labels)))
    ax.plot(pos, y_vals, marker="o", linewidth=2, markersize=4)
    ax.set_xticks(pos)
    step = max(1, len(x_labels) // 12)
    ax.set_xticklabels(
        [lbl if i % step == 0 else "" for i, lbl in enumerate(x_labels)],
        rotation=45, ha="right", fontsize=8,
    )
    ax.set_xlabel(_humanize_column(cols[0]))
    if len(cols) >= 2:
        ax.set_ylabel(_humanize_column(cols[1]))


def _chart_scatter(ax, data: list, cols: list) -> None:
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


def _chart_pie(ax, data: list, cols: list, donut: bool = False) -> None:
    if len(cols) < 2:
        return
    labels = [str(r[cols[0]]) for r in data]
    vals = [abs(_to_float(r[cols[1]]) or 0) for r in data]
    if sum(vals) == 0:
        return
    wedge_kw = {"width": 0.5} if donut else {}
    ax.pie(vals, labels=labels, autopct="%1.1f%%", wedgeprops=wedge_kw)


def _chart_histogram(ax, data: list, cols: list) -> None:
    num = _numeric_cols(data, cols)
    col = num[0] if num else cols[0]
    vals = [_to_float(r[col]) for r in data if _to_float(r.get(col)) is not None]
    if not vals:
        return
    ax.hist(vals, bins=min(20, max(5, len(vals) // 5)), edgecolor="black", alpha=0.7)
    ax.set_xlabel(_humanize_column(col))
    ax.set_ylabel("Count")


def _chart_box(ax, data: list, cols: list) -> None:
    if len(cols) >= 2:
        groups: dict = {}
        for row in data:
            g = str(row[cols[0]])
            v = _to_float(row[cols[1]])
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
        ax.set_xlabel(_humanize_column(cols[0]))
        ax.set_ylabel(_humanize_column(cols[1]))
    else:
        vals = [_to_float(r[cols[0]]) for r in data if _to_float(r.get(cols[0])) is not None]
        if not vals:
            return
        ax.boxplot(vals)
        ax.set_ylabel(_humanize_column(cols[0]))


def _chart_stacked_bar(ax, data: list, cols: list) -> None:
    import numpy as np
    if len(cols) < 3:
        _chart_bar(ax, data, cols)
        return
    pivot: dict = {}
    subcat_order: list = []
    for row in data:
        cat = str(row[cols[0]])
        sub = str(row[cols[1]])
        val = _to_float(row[cols[2]]) or 0
        pivot.setdefault(cat, {})[sub] = val
        if sub not in subcat_order:
            subcat_order.append(sub)
    categories = list(pivot.keys())
    bottom = np.zeros(len(categories))
    for i, sc in enumerate(subcat_order):
        vals = [pivot[cat].get(sc, 0) for cat in categories]
        ax.bar(categories, vals, bottom=bottom, label=sc, color=f"C{i % 10}")
        bottom += np.array(vals)
    ax.set_xticklabels(categories, rotation=45, ha="right", fontsize=8)
    ax.set_xlabel(_humanize_column(cols[0]))
    ax.set_ylabel(_humanize_column(cols[2]))
    ax.legend(title=_humanize_column(cols[1]), bbox_to_anchor=(1.05, 1), loc="upper left", fontsize=8)


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


def _chart_treemap(fig, data: list, cols: list) -> None:
    ax = fig.add_subplot(111)
    if len(cols) < 2:
        return
    labels = [str(r[cols[0]]) for r in data]
    sizes = [abs(_to_float(r[cols[-1]]) or 0) for r in data]
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
    ax.set_title(f"Treemap: {_humanize_column(cols[-1])}")


def _render_chart_image(data: list, chart_type: str, csv_path: Path) -> "Path | None":
    """Render a matplotlib chart from query result data and save as <csv_path>.png.

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
            _chart_stacked_bar(ax, data, cols)
        elif "treemap" in ct:
            _chart_treemap(fig, data, cols)
        elif "line" in ct:
            ax = fig.add_subplot(111)
            _chart_line(ax, data, cols)
        elif "bar" in ct:
            ax = fig.add_subplot(111)
            _chart_bar(ax, data, cols)
        elif "scatter" in ct:
            ax = fig.add_subplot(111)
            _chart_scatter(ax, data, cols)
        elif "donut" in ct:
            ax = fig.add_subplot(111)
            _chart_pie(ax, data, cols, donut=True)
        elif "pie" in ct:
            ax = fig.add_subplot(111)
            _chart_pie(ax, data, cols, donut=False)
        elif "histogram" in ct:
            ax = fig.add_subplot(111)
            _chart_histogram(ax, data, cols)
        elif "box" in ct:
            ax = fig.add_subplot(111)
            _chart_box(ax, data, cols)
        else:
            ax = fig.add_subplot(111)
            _chart_bar(ax, data, cols)

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

    # --- Render chart image (always, regardless of export_target) ---
    # _render_chart_image catches all exceptions internally; a None return means
    # rendering failed and we report that honestly in final_answer.
    chart_png: "Path | None" = _render_chart_image(result_data, state.chart_type, output_path)
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

    final_answer = (
        f"{files_note}\n"
        f"Chart type: {state.chart_type}{reasoning_note}{chart_render_note}{truncation_note}\n\n"
        f"Summary: {summary}{disclosure_note}"
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
    """
    from pathlib import Path

    from utils.data_cleaning import clean_dataset, unresolved_issues_for_record
    from utils.load_data import (
        check_source_freshness,
        compute_and_write_fanout_status,
        compute_file_checksum,
        compute_quality_status,
        ensure_data_quality_status_table,
        ensure_fanout_status_table,
        get_admin_connection,
        load_csv_to_table,
        sanitize_identifier,
        write_data_quality_status,
    )

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
                    import sys as _sys
                    print(
                        f"[checksum] source file for '{table_name}' ({target_csv}) has "
                        f"changed since it was last processed — running a genuinely "
                        f"fresh clean, not reusing a stale prior result.",
                        file=_sys.stderr,
                    )

            cleaning_result = clean_dataset(folder_path, llm=_llm, trigger="auto_redirect")

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
                newly_attempted.append(table_name)
    finally:
        conn.close()

    return {"cleaning_attempted_tables": newly_attempted}


def build_sql_analyst_graph():
    """Wire all nodes into a StateGraph using SQLAnalystState, and compile it.

    Graph shape (normal ask: path, wants_visualization=False):
        START -> curate_question -> add_context
        add_context --(route_after_add_context)--> generate_sql | clean_and_reload
        clean_and_reload -> add_context  (loop; stop condition via cleaning_attempted_tables)
        generate_sql -> is_safe
        is_safe --(route_after_safety_check)--> execute_sql | cancel_sql
        execute_sql --(route_after_execute_sql)--> generate_sql (retry) | represent_final_answer
        cancel_sql -> END
        represent_final_answer -> END

    Additional visualization path (wants_visualization=True):
        add_context --(route_after_add_context)--> determine_chart_type
        determine_chart_type -> generate_sql  (same generate_sql, extended prompt)
        execute_sql --(route_after_execute_sql)--> build_visualization
        build_visualization -> END
    """
    graph = StateGraph(SQLAnalystState)

    graph.add_node("curate_question", curate_question)
    graph.add_node("add_context", add_context)
    graph.add_node("clean_and_reload", clean_and_reload)
    graph.add_node("determine_chart_type", determine_chart_type)
    graph.add_node("generate_sql", generate_sql)
    graph.add_node("is_safe", is_safe)
    graph.add_node("execute_sql", execute_sql)
    graph.add_node("cancel_sql", cancel_sql)
    graph.add_node("represent_final_answer", represent_final_answer)
    graph.add_node("build_visualization", build_visualization)

    graph.add_edge(START, "curate_question")
    graph.add_edge("curate_question", "add_context")
    graph.add_conditional_edges(
        "add_context",
        route_after_add_context,
        {
            "generate_sql": "generate_sql",
            "determine_chart_type": "determine_chart_type",
            "needs_cleaning": "clean_and_reload",
        },
    )
    graph.add_edge("clean_and_reload", "add_context")
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
            "build_visualization": "build_visualization",
        },
    )

    graph.add_edge("cancel_sql", END)
    graph.add_edge("represent_final_answer", END)
    graph.add_edge("build_visualization", END)

    return graph.compile()
