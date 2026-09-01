"""
SQL analyst sub-agent: LangGraph node definitions.
"""

import ast
import csv
import datetime as _dt
import re
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from models.schema import ChartTypeSchema, JudgeSchema, SQLAnalystState
from utils.db import get_app_reader_connection
from utils.llm_pick import pick_llm

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Result parsing ────────────────────────────────────────────────────────────

_DECIMAL_RE = re.compile(r"Decimal\('([^']+)'\)")


def _parse_sql_result(result_str: str) -> list:
    """Parse execute_sql's str() repr of a list of dicts.

    ast.literal_eval alone cannot handle two psycopg2 types that commonly
    appear in query results:
    - Decimal('1.23') — produced for NUMERIC/DECIMAL columns and aggregates
      like SUM/AVG over numeric columns; replaced with the bare float literal
      before parsing.
    - datetime.datetime / datetime.date — produced for timestamp and date
      columns; requires a controlled eval() with the datetime module available.

    Uses eval() with a restricted namespace (no __builtins__) rather than
    ast.literal_eval so these types resolve. The input is always the str()
    repr of our own database execution result — not user-supplied text — and
    the SQL that produced it was already cleared by the is_safe judge, so
    the controlled-eval approach is safe here.
    """
    cleaned = _DECIMAL_RE.sub(r"\1", result_str)
    namespace = {
        "__builtins__": {},
        "datetime": _dt,
        "Decimal": float,
    }
    try:
        result = eval(cleaned, namespace)  # noqa: S307
        if isinstance(result, list):
            return result
        return []
    except Exception:
        return []

_ID_COLUMN_RE = re.compile(r"_id$")


def _is_id_like_column(column_name: str) -> bool:
    """True for column names that look like an entity identifier: exactly \"id\", \
or anything ending in \"_id\" (order_id, customer_id, product_id, ...). This is a naming \
convention, not a hardcoded list of table/column names, so it applies to any dataset that \
follows the common `<entity>_id` convention — which is how Olist (and most relational \
exports) name keys."""
    return column_name == "id" or bool(_ID_COLUMN_RE.search(column_name))


def _detect_fanout_warnings(conn, tables: dict) -> list:
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
                    "(fan-out risk — aggregate before joining)."
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
                WHERE table_schema = %s AND table_name != %s
                ORDER BY table_name, ordinal_position
                """,
                ("public", "_data_quality_status"),
            )
            rows = cur.fetchall()

        # Group columns by table, preserving order
        tables: dict[str, list[tuple[str, str]]] = {}
        for table_name, column_name, data_type in rows:
            tables.setdefault(table_name, []).append((column_name, data_type))

        fanout_warnings = _detect_fanout_warnings(conn, tables)

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
answer (e.g. "the top customer(s)", "which category", "how many") — use LIMIT, aggregation, \
or WHERE clauses so the result set is reasonably sized. Only omit a LIMIT if the question \
genuinely calls for every matching row.
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
  Adjust source columns, table names, and label strings to match the actual schema."""


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
        "Return the two raw numeric columns unaggregated, one row per entity. "
        "Do not GROUP BY or aggregate — the chart engine plots individual points."
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
_TRUNCATION_MARKER = "[TRUNCATED TO FIRST"


def execute_sql(state: SQLAnalystState) -> dict:
    """Node 6: run generated_sql_query against Postgres using the app_reader role.

    On a real database error (bad column, syntax error, etc.) it does not crash —
    it captures the exact error message (prefixed with a sentinel so the routing
    function can distinguish it deterministically from a real result string) and
    increments the attempt counter so the conditional edge after this node can
    route back to generate_sql with that error, capped at MAX_SQL_ATTEMPTS total
    attempts across the whole cycle.

    Result rows are capped at MAX_RESULT_ROWS: an unbounded query (e.g. one with
    no LIMIT that matches a huge fraction of a table) produced a multi-megabyte
    result string in practice, which the summarizer node could not actually see
    in full and ended up fabricating invented statistics over. Truncation is
    reported explicitly in the result string so downstream nodes never mistake
    a partial result for the complete answer.
    """
    attempts = state.sql_attempts + 1
    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
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
                result_str = str([dict(zip(col_names, r)) for r in rows])
                if truncated:
                    result_str = (
                        f"{_TRUNCATION_MARKER} {MAX_RESULT_ROWS} ROWS — the query matched more "
                        f"rows than this; the full result set was NOT retrieved, so do not "
                        f"compute counts/averages/min/max over \"all\" rows from this data]\n"
                        f"{result_str}"
                    )
            else:
                result_str = "(query executed, no rows returned)"
        conn.commit()
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
- If the execution result starts with "[TRUNCATED TO FIRST", it means the database matched \
more rows than were actually retrieved — you are only seeing a partial slice, not the whole \
result set. In that case, do NOT compute or state any count, average, min, max, or other \
aggregate as if it covers "all" matching rows — that would be fabricated from incomplete data. \
Instead say plainly that the result was too large to fully summarize and describe only the \
partial sample you can actually see (e.g. a few example rows), or suggest the question be \
narrowed (e.g. add a LIMIT or filter) to get a complete answer.
- Data-quality note: if the "Data quality notes" section below is non-empty, it means at \
least one table this query actually touched either has no recorded data-quality check, or \
has an unresolved critical (fail-level) data-quality issue. State this plainly but briefly \
— one clear sentence, not alarming — e.g. "Note: this data has an unresolved quality issue \
and the result may be affected." or "Note: this table has never been checked for data \
quality." Do this ONLY when that section is actually non-empty; if it's empty, say nothing \
about data quality at all."""


_TABLE_NAME_RE_CACHE: dict = {}


def _query_touches_table(sql_query: str, table_name: str) -> bool:
    """Whether table_name appears as a real identifier (not a substring of a longer
    word) anywhere in the executed SQL text — quoted ("table") or bare, case-
    insensitive (Postgres folds unquoted identifiers to lowercase, and table_name here
    always comes from information_schema, already lowercase).
    """
    pattern = _TABLE_NAME_RE_CACHE.get(table_name)
    if pattern is None:
        pattern = re.compile(r'(?<![\w"])' + re.escape(table_name) + r'(?![\w"])', re.IGNORECASE)
        _TABLE_NAME_RE_CACHE[table_name] = pattern
    return bool(pattern.search(sql_query))


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

    # Deterministic guard, not just a prompt instruction: a small local model was
    # observed (live, reproduced in tests/test_result_truncation.py) to fabricate
    # counts/averages/min/max over a truncated result anyway, despite an explicit
    # system-prompt rule not to. Rather than trust the LLM to comply, skip the LLM
    # summarization entirely when the result is truncated and report the
    # limitation directly — this cannot be talked out of by the model.
    if state.sql_query_execution_result.startswith(_TRUNCATION_MARKER):
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
        f"Raw SQL execution result: {state.sql_query_execution_result}\n\n"
        f"Data quality notes for tables this query touched:\n{data_quality_section}"
    )
    response = llm.invoke(
        [
            ("system", REPRESENT_FINAL_ANSWER_SYSTEM_PROMPT),
            ("human", human_content),
        ]
    )
    final_answer = _extract_text(response.content).strip()

    return {
        "final_answer": final_answer,
        "messages": [AIMessage(content=final_answer)],
    }


def _humanize_column(col: str) -> str:
    """Convert a SQL alias like 'avg_payment_value' -> 'Average Payment Value'."""
    return col.replace("_", " ").title()


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
    result_data = _parse_sql_result(state.sql_query_execution_result)

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

    if hyper_path is not None:
        files_note = (
            f"Files produced:\n"
            f"  CSV:    {output_path}\n"
            f"  Hyper:  {hyper_path}"
        )
    else:
        files_note = f"Visualization data saved to: {output_path}"

    final_answer = (
        f"{files_note}\n"
        f"Chart type: {state.chart_type}{reasoning_note}\n\n"
        f"Summary: {summary}"
    )

    return {
        "output_file_path": str(output_path),
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
    """
    from pathlib import Path

    from utils.data_cleaning import clean_dataset, unresolved_issues_for_record
    from utils.load_data import (
        compute_quality_status,
        ensure_data_quality_status_table,
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

        for source_folder, table_names in folder_to_tables.items():
            folder_path = Path(source_folder)
            cleaning_result = clean_dataset(folder_path, llm=_llm)

            all_records = {rec.file_name: rec for rec in cleaning_result.cleaned_files}
            all_records.update({rec.file_name: rec for rec in cleaning_result.skipped_files})

            for table_name in table_names:
                # Reverse-map table_name back to its CSV file by sanitized stem.
                target_csv = None
                for csv_path in sorted(folder_path.glob("*.csv")):
                    if sanitize_identifier(csv_path.stem) == table_name:
                        target_csv = csv_path
                        break

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
                write_data_quality_status(
                    conn, table_name, status, issues_found, was_cleaned,
                    source_folder=source_folder,
                )
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
