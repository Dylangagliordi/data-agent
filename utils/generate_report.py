"""Generate an HTML report from a query_log.jsonl entry.

Public interface: generate_report(entry) -> str (path to the written HTML file).

Every section is built ONLY from real, traceable data — never invented:
1. Introduction — dataset name, live row/column counts, the actual question asked,
   and which cleaning issues (if any) were found for the relevant table(s).
3. Data Cleaning — pulled directly from cleaning_log.jsonl for the touched tables;
   narrates the real sequence of what was checked, found, and decided. If no
   cleaning history exists, states that honestly.
4. Question-Specific Data Shaping — deterministic breakdown (from
   utils/sql_transform_extraction.py) of the SQL transformations unique to
   answering THIS question: computed metrics, grouping, minimum sample size,
   ranking/limiting logic, and scope filters — separate from the general,
   dataset-wide cleaning in section 3. States honestly when the query used
   the source data directly with no such shaping.
5. Topic Focus — one pick_llm("cheap") call grounded in the actual question + result
   + cleaning context, explaining why the question matters AND why cleaning was
   necessary before the result could be trusted.
6. Visualization — ONLY for visualize: entries with chart_type/output_file_path set.
   Embeds the chart image directly when chart_image_path is present. Derives
   plain-English "Assumptions" from the real WHERE/HAVING clauses in generated_sql_query.
7. Summary — one pick_llm("cheap") call synthesizing only from assembled facts,
   narrating the full process from data quality check to final answer.
"""

import base64
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_QUERY_LOG_PATH = _PROJECT_ROOT / "logs" / "query_log.jsonl"
_CLEANING_LOG_PATH = _PROJECT_ROOT / "logs" / "cleaning_log.jsonl"
_REPORTS_DIR = _PROJECT_ROOT / "reports"


# ── Table detection ────────────────────────────────────────────────────────────

def _all_table_names() -> list:
    """Return every table name in the public schema via app_reader (read-only)."""
    from utils.db import get_app_reader_connection
    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY table_name",
                ("public",),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _detect_tables_in_sql(sql: str, known_tables: list) -> list:
    """Return the subset of known_tables whose names appear (as whole words) in sql."""
    sql_upper = sql.upper()
    return [t for t in known_tables if re.search(r"\b" + re.escape(t.upper()) + r"\b", sql_upper)]


# ── Database metadata ──────────────────────────────────────────────────────────

def _table_metadata(table_names: list) -> list:
    """For each table name, return a dict with row_count and columns list.
    Queries live from information_schema + a real COUNT(*) per table.
    """
    if not table_names:
        return []
    from utils.db import get_app_reader_connection
    conn = get_app_reader_connection()
    meta = []
    try:
        with conn.cursor() as cur:
            for tname in table_names:
                # Column names and types from information_schema.
                cur.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                    ("public", tname),
                )
                columns = [{"name": row[0], "type": row[1]} for row in cur.fetchall()]

                # Exact row count.
                cur.execute(f'SELECT COUNT(*) FROM "{tname}"')  # noqa: S608 — table name validated against information_schema
                row_count = cur.fetchone()[0]

                meta.append({"table": tname, "row_count": row_count, "columns": columns})
    finally:
        conn.close()
    return meta


# ── Cleaning log lookup ────────────────────────────────────────────────────────

def _cleaning_entries_for_tables(table_names: list) -> dict:
    """Return the MOST RECENT cleaning_log.jsonl file-level record for each table_name
    in table_names. Returns a dict mapping table_name -> file-record dict (or None).
    """
    result = {t: None for t in table_names}
    if not _CLEANING_LOG_PATH.exists() or not table_names:
        return result

    # cleaning_log.jsonl stores table_name from the raw file stem (e.g. "Uncleaned_DS_jobs")
    # while Postgres (and touched_tables from information_schema) uses lowercase names
    # (e.g. "uncleaned_ds_jobs"). Normalize both sides at lookup time so real log entries
    # are found regardless of the original file's casing — the log itself is never changed.
    normalized_lookup = {t.lower().strip(): t for t in table_names}

    lines = _CLEANING_LOG_PATH.read_text().splitlines()
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        for file_rec in entry.get("files", []):
            tname = file_rec.get("table_name", "")
            tname_normalized = tname.lower().strip()
            if tname_normalized in normalized_lookup:
                original_key = normalized_lookup[tname_normalized]
                result[original_key] = (entry, file_rec)
    return result


# ── Before/after cleaning comparison ─────────────────────────────────────────

def _parse_placeholder_issue(issue_text: str) -> "dict | None":
    """Parse a placeholder-value issue to extract column, count, and placeholder values.

    Returns {"column": str, "count": int, "placeholders": list[str]} or None.
    Matches the standard format produced by utils/data_cleaning.py:
      "Placeholder values: column 'ColName' has N value(s) that look like
       placeholders standing in for real data (['val1', 'val2']), ..."
    """
    m = re.search(
        r"column\s+'([^']+)'\s+has\s+(\d+)\s+value\(s\).*?\(\[(.+?)\]\)",
        issue_text, re.DOTALL,
    )
    if not m:
        return None
    col = m.group(1)
    count = int(m.group(2))
    placeholders_raw = m.group(3)
    placeholders = re.findall(r"'([^']*)'", placeholders_raw)
    if not placeholders:
        placeholders = [v.strip() for v in placeholders_raw.split(",") if v.strip()]
    return {"column": col, "count": count, "placeholders": placeholders}


def _normalize_col_name(s: str) -> str:
    """Normalize a column name for cross-format comparison: lowercase, non-
    alphanumeric runs collapsed to a single underscore. Lets a cleaning-log
    column like 'Type of ownership' match a Postgres column like
    'type_of_ownership'."""
    return re.sub(r"[^a-z0-9]+", "_", str(s).lower()).strip("_")


def _detect_touched_columns(sql: str, column_names: list) -> set:
    """Return the normalized subset of column_names that actually appear (as
    whole words) in sql. Used to scope the cleaning-impact comparison to
    columns the executed query actually used, instead of picking whichever
    fail-level issue affected the most rows table-wide regardless of
    relevance to this specific analysis."""
    if not sql:
        return set()
    sql_upper = sql.upper()
    return {
        _normalize_col_name(c)
        for c in column_names
        if re.search(r"\b" + re.escape(str(c).upper()) + r"\b", sql_upper)
    }


def _biggest_fail_placeholder(file_rec: dict, touched_columns: "set | None" = None) -> "dict | None":
    """Return the parsed placeholder issue with the highest affected-value count
    among all fail-severity issues in file_rec["issues_found"], or None if there
    is no parseable placeholder issue.

    When touched_columns is a non-empty set, only issues whose column actually
    appears in that set are considered — this scopes the comparison to columns
    the executed query actually touched, rather than surfacing an unrelated
    column just because it had the largest table-wide impact.
    """
    fail_issues = [i for i in file_rec.get("issues_found", []) if i.get("severity") == "fail"]
    parsed = []
    for issue in fail_issues:
        p = _parse_placeholder_issue(issue.get("issue", ""))
        if p:
            parsed.append(p)
    if not parsed:
        return None
    if touched_columns:
        scoped = [p for p in parsed if _normalize_col_name(p["column"]) in touched_columns]
        if scoped:
            return max(scoped, key=lambda p: p["count"])
        return None
    return max(parsed, key=lambda p: p["count"])


def _compute_before_after(db_table: str, biggest: dict) -> "dict | None":
    """Run two real COUNT queries against the live table to produce a before/after
    comparison for the given placeholder issue.

    db_table: the Postgres table name (lowercase, as found in information_schema).
    biggest:  result of _biggest_fail_placeholder — contains column, count, placeholders.

    Returns a dict with keys: clean, unclean, column, placeholders_str, count,
    resolved (bool), explanation (str). Returns None if any DB step fails or the
    column can't be matched.

    Uses only read-only app_reader connections — no writes.
    """
    from utils.db import get_app_reader_connection

    col_raw = biggest["column"]
    placeholders = biggest["placeholders"]
    affected_count = biggest["count"]

    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            # Case-insensitive column lookup against information_schema.
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s",
                (db_table,),
            )
            db_columns = [row[0] for row in cur.fetchall()]

        matching_col = next(
            (c for c in db_columns if c.lower() == col_raw.lower()), None
        )
        if not matching_col:
            return None

        ph_str = ", ".join(f"'{p}'" for p in placeholders)
        placeholders_sql = ", ".join(["%s"] * len(placeholders))

        with conn.cursor() as cur:
            # Check whether placeholder values still exist in the table.
            cur.execute(
                f'SELECT COUNT(*) FROM "{db_table}" '
                f'WHERE "{matching_col}" IN ({placeholders_sql})',
                tuple(placeholders),
            )
            (existing_count,) = cur.fetchone()

        if existing_count > 0:
            # Issue unresolved — placeholders still in the table.
            with conn.cursor() as cur:
                cur.execute(
                    f'SELECT COUNT(*) FROM "{db_table}" '
                    f'WHERE "{matching_col}" IS NOT NULL '
                    f'AND "{matching_col}" NOT IN ({placeholders_sql})',
                    tuple(placeholders),
                )
                (clean_count,) = cur.fetchone()

                cur.execute(f'SELECT COUNT(*) FROM "{db_table}"')
                (unclean_count,) = cur.fetchone()

            diff = unclean_count - clean_count
            explanation = (
                f"Rows with a valid {col_raw} (excluding placeholder {ph_str}): "
                f"{clean_count:,}. "
                f"Total rows including placeholders: {unclean_count:,}. "
                f"Difference: {diff:,} rows where {col_raw} = {ph_str}."
            )
            return {
                "clean": clean_count,
                "unclean": unclean_count,
                "column": col_raw,
                "placeholders_str": ph_str,
                "count": affected_count,
                "resolved": False,
                "explanation": explanation,
            }
        else:
            # Issue resolved — placeholders removed or replaced with NULL.
            with conn.cursor() as cur:
                cur.execute(f'SELECT COUNT(*) FROM "{db_table}"')
                (clean_count,) = cur.fetchone()

            unclean_sim = clean_count + affected_count
            explanation = (
                f"After cleaning: {clean_count:,} rows in {db_table}. "
                f"Before cleaning: approximately {unclean_sim:,} rows — "
                f"cleaning removed {affected_count:,} rows where "
                f"{col_raw} = {ph_str}."
            )
            return {
                "clean": clean_count,
                "unclean": unclean_sim,
                "column": col_raw,
                "placeholders_str": ph_str,
                "count": affected_count,
                "resolved": True,
                "explanation": explanation,
            }
    except Exception:
        return None
    finally:
        conn.close()


# ── Query assumption extraction ───────────────────────────────────────────────

# Placeholder-like literal values that signal a non-obvious exclusion filter.
_PLACEHOLDER_LITERALS = {"-1", "n/a", "na", "unknown", "none", "#n/a", "0", ""}


def _extract_query_assumptions(sql_query: str) -> list:
    """Derive plain-English assumption statements from the real WHERE/HAVING clauses.

    Only surfaces non-obvious filters — placeholder-value exclusions
    (col <> '-1'), regex/LIKE pattern matches, and HAVING COUNT thresholds.
    Standard NULL checks (col IS NOT NULL) are considered obvious and omitted.

    All statements are derived mechanically from the actual SQL text; nothing
    is invented or inferred from context outside the query.
    """
    if not sql_query:
        return []

    assumptions = []
    seen: set = set()  # dedup

    def add(text: str) -> None:
        if text not in seen:
            seen.add(text)
            assumptions.append(text)

    # 1. Placeholder-value exclusion: col <> 'value' or col != 'value'
    for col, val in re.findall(
        r"\b(\w+)\s*(?:<>|!=)\s*'([^']*)'", sql_query, re.IGNORECASE
    ):
        if val.lower() in _PLACEHOLDER_LITERALS:
            add(
                f"Rows where {col} equals '{val}' (a placeholder value) "
                f"were excluded from this analysis."
            )

    # 2. Multi-value NOT IN exclusion with string literals.
    for col, values_str in re.findall(
        r"\b(\w+)\s+NOT\s+IN\s*\(([^)]+)\)", sql_query, re.IGNORECASE
    ):
        values = re.findall(r"'([^']*)'", values_str)
        if values:
            vals_fmt = ", ".join(f"'{v}'" for v in values)
            add(f"Rows where {col} is {vals_fmt} were excluded.")

    # 3. Regex/pattern match: col ~ 'pattern'
    for col, pattern in re.findall(
        r"\b(\w+)\s*~\s*'([^']*)'", sql_query, re.IGNORECASE
    ):
        add(
            f"Only rows where {col} matches the pattern '{pattern}' were included "
            f"(rows not matching this format were excluded)."
        )

    # 4. LIKE pattern match
    for col, pattern in re.findall(
        r"\b(\w+)\s+LIKE\s+'([^']*)'", sql_query, re.IGNORECASE
    ):
        add(f"Only rows where {col} matches the pattern '{pattern}' were included.")

    # 5. HAVING COUNT(*) >= N threshold
    m = re.search(
        r"\bHAVING\b.*?\bCOUNT\b\s*\(\s*\*?\s*\)\s*>=?\s*(\d+)",
        sql_query, re.IGNORECASE | re.DOTALL,
    )
    if m:
        add(
            f"Groups with fewer than {m.group(1)} rows were excluded "
            f"to ensure statistical reliability."
        )

    return assumptions


# ── Results table (the single deterministic source of truth for the answer) ────

def _humanize_col(col: str) -> str:
    """Convert a SQL alias like 'avg_payment_value' -> 'Average Payment Value'.
    Mirrors agents.sql_analyst._humanize_column exactly (duplicated here, not
    imported, to avoid a utils -> agents import cycle) so column labels match
    the CSV/chart the query already produced.
    """
    return col.replace("_", " ").title()


def _parse_result_rows(result_str: str) -> tuple[list, bool]:
    """Parse execute_sql's {"columns": [...], "rows": [[...], ...], "truncated": bool}
    JSON payload into a list of row dicts. Returns ([], False) for an error string
    or unparseable input. Deliberately independent of agents.sql_analyst's own
    _parse_sql_result (same shape, no cross-package import needed for this).
    """
    if not result_str or result_str.startswith("SQL_EXECUTION_ERROR"):
        return [], False
    try:
        payload = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return [], False
    if not isinstance(payload, dict):
        return [], False
    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    truncated = bool(payload.get("truncated", False))
    if not columns or not isinstance(rows, list):
        return [], truncated
    return [dict(zip(columns, r)) for r in rows], truncated


def _section_results(entry: dict) -> str:
    """Render the actual query result as a plain HTML table — the single
    deterministic, checkable source of truth for the answer. Every other
    section (chart, Topic Focus prose, Summary prose) can be verified against
    this table; nothing here is synthesized or interpreted, it's the real rows
    the executed SQL returned.
    """
    result_str = entry.get("sql_query_execution_result", "")
    rows, was_truncated = _parse_result_rows(result_str)

    if not rows:
        return (
            "<h2>Results</h2>\n"
            "<div class='note'>No result rows are available for this entry "
            "(the query returned nothing, or this entry predates structured "
            "result logging).</div>"
        )

    cols = list(rows[0].keys())
    human_cols = [_humanize_col(c) for c in cols]

    parts = ["<h2>Results</h2>"]
    if was_truncated:
        parts.append(
            "<div class='note'>This result was capped before reaching this table — "
            "the rows below are a partial sample, not the complete result set.</div>"
        )
    parts.append("<table><thead><tr>" + "".join(f"<th>{_esc(h)}</th>" for h in human_cols) + "</tr></thead><tbody>")
    for row in rows:
        cells = "".join(f"<td>{_esc(row.get(c))}</td>" for c in cols)
        parts.append(f"<tr>{cells}</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _results_text_for_prompt(entry: dict, max_rows: int = 15) -> str:
    """Plain-text rendering of the same rows _section_results shows, for
    grounding the two LLM prose sections in the exact same numbers a reader
    sees in the Results table above — rather than each independently re-parsing
    a truncated raw JSON snippet of sql_query_execution_result.
    """
    rows, was_truncated = _parse_result_rows(entry.get("sql_query_execution_result", ""))
    if not rows:
        return ""
    cols = list(rows[0].keys())
    human_cols = [_humanize_col(c) for c in cols]
    lines = [", ".join(human_cols)]
    for row in rows[:max_rows]:
        lines.append(", ".join(str(row.get(c)) for c in cols))
    text = "\n".join(lines)
    if was_truncated:
        text += "\n(result was truncated — shown rows are a partial sample)"
    elif len(rows) > max_rows:
        text += f"\n(... {len(rows) - max_rows} more row(s) not shown here)"
    return text


# ── Cleaning narrative helpers ─────────────────────────────────────────────────

def _issue_category(issue_text: str) -> str:
    """Return the broad category label for an issue string."""
    for prefix in [
        "Placeholder values",
        "Column misalignment",
        "Column header issues",
        "Duplicate rows",
        "Duplicate values",
        "Invalid values",
        "Currency/unit symbols",
    ]:
        if issue_text.startswith(prefix):
            return prefix
    return "Other issues"


def _cleaning_context_text(cleaning_map: dict) -> str:
    """Build a plain-text cleaning summary for LLM prompt context.

    Only includes tables that actually have cleaning history.
    """
    parts = []
    for tname, rec in cleaning_map.items():
        if rec is None:
            continue
        entry_meta, file_rec = rec
        issues = file_rec.get("issues_found", [])
        resolved = file_rec.get("issues_resolved", [])
        unresolved = file_rec.get("issues_still_unresolved", [])
        fc = sum(1 for i in issues if i.get("severity") == "fail")
        wc = sum(1 for i in issues if i.get("severity") == "warn")
        fail_cats = sorted({_issue_category(i.get("issue", "")) for i in issues if i.get("severity") == "fail"})
        warn_cats = sorted({_issue_category(i.get("issue", "")) for i in issues if i.get("severity") == "warn"})
        parts.append(
            f"Table '{tname}': {len(issues)} issues found "
            f"({fc} critical — {', '.join(fail_cats) or 'none'}; "
            f"{wc} advisory — {', '.join(warn_cats) or 'none'}). "
            f"{len(resolved)} resolved, {len(unresolved)} still unresolved."
        )
    return " | ".join(parts)


# ── HTML helpers ───────────────────────────────────────────────────────────────

def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _slug(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", question.lower())[:40].strip("_")


_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:940px;margin:48px auto;padding:0 24px;color:#222;line-height:1.6}
h1{color:#111;border-bottom:2px solid #ddd;padding-bottom:12px;font-size:1.6em}
h2{color:#1a3a5c;margin-top:40px;font-size:1.2em;border-left:4px solid #4a90d9;padding-left:10px}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:0.93em}
th,td{border:1px solid #ddd;padding:7px 12px;text-align:left;vertical-align:top}
th{background:#f2f5f9;font-weight:600}
.meta{color:#666;font-size:0.88em;margin-top:-8px}
pre{font-family:'SFMono-Regular',Consolas,monospace;background:#f6f8fa;padding:14px;border-radius:6px;white-space:pre-wrap;font-size:0.88em;border:1px solid #e0e0e0}
.note{background:#fff8e1;border-left:4px solid #f9a825;padding:10px 14px;border-radius:3px;margin:12px 0}
.ok{color:#2e7d32;font-weight:600}
.bad{color:#c62828;font-weight:600}
ul{margin:8px 0;padding-left:22px}
li{margin:3px 0}
.issue-block{border-left:3px solid #c62828;background:#fff8f8;padding:10px 14px;margin:12px 0;border-radius:3px}
.resolved-block{border-left:3px solid #2e7d32;background:#f8fff8;padding:10px 14px;margin:12px 0;border-radius:3px}
"""


def _html_page(title: str, body: str, generated_at: str) -> str:
    return (
        f"<!DOCTYPE html>\n<html lang='en'>\n<head>\n"
        f"<meta charset='UTF-8'>\n"
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        f"</head>\n<body>\n"
        f"<h1>{_esc(title)}</h1>\n"
        f"<p class='meta'>Generated {_esc(generated_at)}</p>\n"
        f"{body}\n"
        f"</body>\n</html>\n"
    )


# ── Section builders ───────────────────────────────────────────────────────────

def _section_introduction(entry: dict, table_meta: list) -> str:
    question = entry.get("user_question") or entry.get("curated_question") or ""
    route = entry.get("route_response", "")

    rows = [f"<p><strong>Question:</strong> {_esc(question)}</p>"]
    rows.append(f"<p><strong>Run type:</strong> {_esc(route)}</p>")

    if table_meta:
        rows.append("<h3 style='margin-top:18px'>Dataset tables</h3>")
        rows.append(
            "<table><thead><tr>"
            "<th>Table</th><th>Rows</th><th>Columns</th>"
            "</tr></thead><tbody>"
        )
        for tm in table_meta:
            col_names = ", ".join(c["name"] for c in tm["columns"])
            rows.append(
                f"<tr><td>{_esc(tm['table'])}</td>"
                f"<td>{tm['row_count']:,}</td>"
                f"<td>{len(tm['columns'])} — {_esc(col_names)}</td></tr>"
            )
        rows.append("</tbody></table>")
    else:
        rows.append("<p>No known tables were detected in the generated SQL.</p>")

    return "<h2>Introduction</h2>\n" + "\n".join(rows)


def _section_data_cleaning(cleaning_map: dict, sql_query: str = "", table_columns: "dict | None" = None) -> str:
    """Narrative data cleaning section.

    Tells the story in order: what was audited → what was found (by severity) →
    what was decided for each critical issue → advisory fixes → numeric impact.
    Same real data as before; restructured as a process, not a flat list.

    sql_query + table_columns (table_name -> real column names from
    information_schema), when both provided, scope the Before/After Cleaning
    Impact table (Step 5 below) to a column the executed query actually
    touched, instead of surfacing whichever fail-level issue affected the most
    rows table-wide regardless of relevance to this specific analysis.
    """
    table_columns = table_columns or {}
    parts = ["<h2>Data Cleaning</h2>"]

    if not cleaning_map:
        parts.append(
            "<div class='note'>No tables were identified in the query, "
            "so data-cleaning history cannot be looked up.</div>"
        )
        return "\n".join(parts)

    for tname, rec in cleaning_map.items():
        parts.append(f"<h3 style='margin-top:20px'>{_esc(tname)}</h3>")
        if rec is None:
            parts.append(
                "<div class='note'>This table has no cleaning history in this system — "
                "it has never been processed through clean_dataset(). "
                "Data quality has not been verified and no cleaning was performed.</div>"
            )
            continue

        entry_meta, file_rec = rec
        trigger = entry_meta.get("trigger", "unknown")
        ts = entry_meta.get("timestamp", "")
        status = file_rec.get("status", "")
        rb = file_rec.get("row_count_before")
        ra = file_rec.get("row_count_after")
        row_loss = file_rec.get("row_loss_flagged", False)

        issues_found = file_rec.get("issues_found", [])
        fail_issues_found = [i for i in issues_found if i.get("severity") == "fail"]
        warn_issues_found = [i for i in issues_found if i.get("severity") == "warn"]
        fc = len(fail_issues_found)
        wc = len(warn_issues_found)
        total = len(issues_found)

        # ── Step 1: What was audited ──────────────────────────────────────────
        if total == 0:
            parts.append(
                f"<p>The dataset was scanned for data quality issues "
                f"(trigger: <em>{_esc(trigger)}</em>, {_esc(ts)}). "
                f"No issues were detected — the data loaded cleanly.</p>"
            )
            continue

        issue_summary_parts = []
        if fc:
            issue_summary_parts.append(f"<span class='bad'>{fc} critical (fail-level)</span>")
        if wc:
            issue_summary_parts.append(f"{wc} advisory (warn-level)")

        parts.append(
            f"<p>The dataset was scanned for data quality issues "
            f"(trigger: <em>{_esc(trigger)}</em>, {_esc(ts)}). "
            f"The scanner found <strong>{total} issue{'s' if total != 1 else ''}</strong>: "
            f"{' and '.join(issue_summary_parts)}. "
            f"Critical issues were addressed first because they can silently corrupt "
            f"aggregated results; advisory issues were resolved afterward.</p>"
        )

        # ── Step 2: Critical issues — checked and decided in order ────────────
        if fail_issues_found:
            parts.append(
                "<h4 style='margin-top:18px'>Critical Issues — Checked and Resolved in Sequence</h4>"
            )

            # Build a lookup from issue text → fail_issues detail record (ungrouped
            # issues), and a second lookup from issue text → the FailBatchRecord dict
            # it belongs to, if any (Spec 4 — a group of 2+ issues that shared an
            # identical, mechanically-verified treatment and got one combined fix).
            fail_detail_map = {fi["issue"]: fi for fi in file_rec.get("fail_issues", [])}
            batch_by_issue = {}
            for batch in file_rec.get("fail_batches", []):
                for batch_issue_text in batch.get("issues", []):
                    batch_by_issue[batch_issue_text] = batch
            resolved_set = set(file_rec.get("issues_resolved", []))
            rendered_batch_ids: set = set()

            idx = 0
            for iss in fail_issues_found:
                issue_text = iss.get("issue", "")
                batch = batch_by_issue.get(issue_text)

                if batch is not None:
                    # One combined block per batch — replaces the N-near-identical-
                    # blocks problem: render it once, the first time any of its
                    # member issues is encountered, listing every issue it covered.
                    if id(batch) in rendered_batch_ids:
                        continue
                    rendered_batch_ids.add(id(batch))
                    idx += 1
                    batch_issues = batch.get("issues", [])
                    is_resolved = batch.get("status") == "resolved"
                    reasoning = batch.get("reasoning_comments", [])

                    outcome_label = (
                        "<span class='ok'>&#x2713; Resolved</span>"
                        if is_resolved
                        else "<span class='bad'>&#x2717; Unresolved</span>"
                    )
                    block_cls = "resolved-block" if is_resolved else "issue-block"

                    parts.append(f"<div class='{block_cls}'>")
                    parts.append(
                        f"<p><strong>Issue {idx} "
                        f"(identical fix applied across {len(batch_issues)} columns):</strong></p>"
                        "<ul>" + "".join(f"<li>{_esc(bi)}</li>" for bi in batch_issues) + "</ul>"
                        f"<p><strong>Outcome:</strong> {outcome_label}</p>"
                    )
                else:
                    idx += 1
                    detail = fail_detail_map.get(issue_text, {})
                    is_resolved = issue_text in resolved_set
                    reasoning = detail.get("reasoning_comments", [])

                    outcome_label = (
                        "<span class='ok'>&#x2713; Resolved</span>"
                        if is_resolved
                        else "<span class='bad'>&#x2717; Unresolved</span>"
                    )
                    block_cls = "resolved-block" if is_resolved else "issue-block"

                    parts.append(f"<div class='{block_cls}'>")
                    parts.append(
                        f"<p><strong>Issue {idx}:</strong> {_esc(issue_text)}</p>"
                        f"<p><strong>Outcome:</strong> {outcome_label}</p>"
                    )

                if reasoning:
                    clean_comments = "\n".join(
                        re.sub(r"^#\s*", "", c) for c in reasoning
                    ).strip()
                    if clean_comments:
                        parts.append(
                            f"<p><strong>Fix reasoning:</strong></p>"
                            f"<pre>{_esc(clean_comments)}</pre>"
                        )
                parts.append("</div>")

        # ── Step 3: Advisory issues — handled after critical fixes ─────────────
        if warn_issues_found:
            parts.append(
                "<h4 style='margin-top:18px'>Advisory Issues — Addressed After Critical Fixes</h4>"
            )
            resolved_set = set(file_rec.get("issues_resolved", []))
            parts.append("<ul>")
            for iss in warn_issues_found:
                issue_text = iss.get("issue", "")
                is_resolved = issue_text in resolved_set
                marker = "<span class='ok'>&#x2713;</span>" if is_resolved else "&#x25cb;"
                parts.append(f"<li>{marker} {_esc(issue_text)}</li>")
            parts.append("</ul>")

            # One or more warn batches (Spec 4 — a file can now have multiple, when
            # some warn issues shared a real treatment signature separately from the
            # rest); render each batch's own fix reasoning in turn.
            for wb in file_rec.get("warn_batches") or []:
                warn_reasoning = wb.get("reasoning_comments", [])
                if warn_reasoning:
                    clean_warn = "\n".join(
                        re.sub(r"^#\s*", "", c) for c in warn_reasoning
                    ).strip()
                    if clean_warn:
                        parts.append(
                            f"<p><strong>Fix reasoning ({len(wb.get('issues', []))} issue(s)):</strong></p>"
                            f"<pre>{_esc(clean_warn)}</pre>"
                        )

        # ── Step 4: Row counts before / after ─────────────────────────────────
        if rb is not None and ra is not None:
            parts.append("<h4 style='margin-top:18px'>Row Count: Before vs After</h4>")
            parts.append(
                "<table><thead><tr>"
                "<th>Before cleaning</th><th>After cleaning</th><th>Note</th>"
                "</tr></thead><tbody>"
                f"<tr><td>{rb:,} rows</td><td>{ra:,} rows</td>"
                f"<td>{'<span class=\"bad\">Row loss flagged (&ge;20%)</span>' if row_loss else 'Within acceptable range'}</td>"
                f"</tr></tbody></table>"
            )

        # ── Step 5: Concrete before/after impact for the biggest fail issue ───
        parts.append("<h4 style='margin-top:18px'>Before/After Cleaning Impact</h4>")
        touched_cols = _detect_touched_columns(sql_query, table_columns.get(tname, []))
        biggest = _biggest_fail_placeholder(file_rec, touched_columns=touched_cols)
        if biggest is None:
            parts.append(
                "<div class='note'>No placeholder-value fail issue was found for a column "
                "this query actually touched — a concrete before/after numeric comparison "
                "cannot be automatically computed for other issue types (e.g. column "
                "misalignment) or for columns not relevant to this specific analysis.</div>"
            )
        else:
            try:
                comparison = _compute_before_after(tname, biggest)
            except Exception:
                comparison = None

            if comparison is None:
                parts.append(
                    "<div class='note'>A before/after comparison could not be computed "
                    f"for the column '{_esc(biggest['column'])}' — the column may not "
                    "exist in the current table schema.</div>"
                )
            else:
                state_label = (
                    "Cleaning resolved this issue (placeholder values removed)"
                    if comparison["resolved"]
                    else "Issue still unresolved (placeholder values remain in table)"
                )
                parts.append(
                    "<table><thead><tr>"
                    "<th>Scenario</th><th>Row count</th><th>Note</th>"
                    "</tr></thead><tbody>"
                    f"<tr><td>After cleaning (current)</td>"
                    f"<td>{comparison['clean']:,}</td>"
                    f"<td>Rows with valid <code>{_esc(comparison['column'])}</code></td></tr>"
                    f"<tr><td>Before cleaning (simulated)</td>"
                    f"<td>{comparison['unclean']:,}</td>"
                    f"<td>Includes {comparison['count']:,} rows where "
                    f"<code>{_esc(comparison['column'])}</code> = "
                    f"{_esc(comparison['placeholders_str'])}</td></tr>"
                    "</tbody></table>"
                )
                parts.append(
                    f"<p class='note'><strong>Impact:</strong> {_esc(comparison['explanation'])} "
                    f"({_esc(state_label)}.)</p>"
                )

    return "\n".join(parts)


def _section_question_transformations(sql_query: str) -> str:
    """Render the deterministic, question-specific shaping this query
    applies — the metrics computed, how rows were grouped, any minimum-
    sample threshold, the ranking/limiting logic, and any scope filter that
    narrows the dataset to what this specific question is about. This is
    separate from — and comes after — the general dataset-wide cleaning
    covered in `_section_data_cleaning`; nothing here overlaps with that
    section's placeholder/duplicate/misalignment fixes. Every fact comes
    from `extract_question_transformations`, itself a pure parse of the
    real executed SQL — nothing invented or LLM-derived.
    """
    from utils.sql_transform_extraction import (
        extract_question_transformations,
        has_any_transformation,
    )

    parts = ["<h2>Question-Specific Data Shaping</h2>"]
    if not sql_query:
        parts.append(
            "<div class='note'>No SQL was generated for this entry, so there is no "
            "question-specific shaping to describe.</div>"
        )
        return "\n".join(parts)

    t = extract_question_transformations(sql_query)
    if not has_any_transformation(t):
        parts.append(
            "<div class='note'>This query used the source data directly with no "
            "additional shaping beyond the general cleaning above — no computed "
            "metrics, grouping, sampling threshold, ranking, or scope filter was "
            "needed to answer this specific question.</div>"
        )
        return "\n".join(parts)

    parts.append(
        "<p>This is separate from the general cleaning above — these are the "
        "transformations applied specifically to shape the data for <em>this</em> "
        "question and its chart, not fixes to bad data.</p>"
    )

    if t["cte_steps"]:
        chips = " &rarr; ".join(_esc(s) for s in t["cte_steps"])
        parts.append(
            f"<p><strong>Built in {len(t['cte_steps'])} step(s):</strong> {chips}</p>"
        )

    if t["scope_filters"]:
        parts.append("<h4 style='margin-top:16px'>Scoped to this question</h4><ul>")
        for f in t["scope_filters"]:
            parts.append(f"<li>{_esc(f)}</li>")
        parts.append("</ul>")

    if t["computed_columns"]:
        parts.append("<h4 style='margin-top:16px'>Metrics computed for this answer</h4>")
        parts.append(
            "<table><thead><tr><th>Metric</th><th>How it was computed</th>"
            "</tr></thead><tbody>"
        )
        for c in t["computed_columns"]:
            parts.append(
                f"<tr><td>{_esc(_humanize_col(c['alias']))}</td>"
                f"<td><code>{_esc(c['expression'])}</code></td></tr>"
            )
        parts.append("</tbody></table>")

    if t["grouping_columns"]:
        for group in t["grouping_columns"]:
            cols_fmt = ", ".join(_esc(c) for c in group)
            parts.append(
                f"<p><strong>Grouped by:</strong> {cols_fmt} — each result row "
                f"summarizes one of these groups, not one raw source record.</p>"
            )

    if t["having_threshold"] is not None:
        parts.append(
            f"<p><strong>Minimum sample size:</strong> groups with fewer than "
            f"{t['having_threshold']} underlying rows were excluded from the "
            f"ranking, to avoid basing an average on too little data.</p>"
        )

    if t["ranking_stages"]:
        parts.append("<h4 style='margin-top:16px'>Ranking &amp; limiting logic</h4><ol>")
        for stage in t["ranking_stages"]:
            if stage["limit"] is not None:
                parts.append(
                    f"<li>Ranked by <code>{_esc(stage['order_by'])}</code>, "
                    f"then capped to the top {stage['limit']} result(s).</li>"
                )
            else:
                parts.append(
                    f"<li>Presented in order of <code>{_esc(stage['order_by'])}</code>.</li>"
                )
        parts.append("</ol>")

    return "\n".join(parts)


def _section_topic_focus(entry: dict, llm, cleaning_context: str = "") -> str:
    question = entry.get("user_question") or entry.get("curated_question") or ""
    results_text = _results_text_for_prompt(entry)

    cleaning_clause = ""
    if cleaning_context:
        cleaning_clause = (
            f"Before the analysis, the following data quality issues were found and addressed: "
            f"{cleaning_context}\n\n"
            f"Explain why each of those cleaning steps was necessary before this specific "
            f"analysis could be trusted — which issues, if left uncleaned, would have "
            f"distorted or invalidated this result.\n\n"
        )

    prompt = (
        f"A data analyst asked this question: \"{question}\"\n\n"
        f"The query returned this exact result (already shown to the reader in a table "
        f"above — do not restate these numbers, reference them only in service of your "
        f"explanation):\n{results_text or '(no rows returned)'}\n\n"
        f"{cleaning_clause}"
        f"In 2-4 sentences, explain why this is a meaningful question to ask of this dataset. "
        f"Be specific and grounded in what the data actually showed — do not invent "
        f"business context or mention anything not directly present in the question, result, "
        f"and cleaning context above. Do not repeat the specific numbers already shown in the "
        f"Results table — focus on why the question matters and, if cleaning was performed, "
        f"why it mattered here. "
        f"Write plain prose only — no markdown headers, no bullet points, no formatting symbols."
    )
    explanation = llm.invoke([("human", prompt)]).content
    if isinstance(explanation, list):
        explanation = "".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in explanation if not (isinstance(b, dict) and b.get("type") == "thinking")
        )

    return (
        "<h2>Topic Focus</h2>\n"
        f"<p><strong>Question:</strong> {_esc(question)}</p>\n"
        f"<p>{_esc(explanation)}</p>"
    )


def _section_visualization(entry: dict) -> str:
    chart_type = entry.get("chart_type", "")
    chart_type_source = entry.get("chart_type_source", "")
    chart_type_reasoning = entry.get("chart_type_reasoning", "")
    output_file_path = entry.get("output_file_path", "")
    chart_image_path = entry.get("chart_image_path", "")
    generated_sql = entry.get("generated_sql_query", "")

    if not chart_type or not output_file_path:
        return ""

    lines = [
        "<h2>Visualization</h2>",
        f"<p><strong>Chart type:</strong> {_esc(chart_type)}</p>",
        f"<p><strong>Selection method:</strong> {_esc(chart_type_source)}</p>",
    ]

    # Only emit reasoning when the chart type was derived by the model, not named by the user.
    if chart_type_source == "reasoned" and chart_type_reasoning:
        lines.append(f"<p><strong>Reasoning:</strong> {_esc(chart_type_reasoning)}</p>")

    if chart_image_path:
        img_path = Path(chart_image_path)
        if img_path.exists():
            b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
            lines.append(
                f'<img src="data:image/png;base64,{b64}" '
                f'style="max-width:100%;height:auto;border:1px solid #ddd;'
                f'border-radius:6px;margin:12px 0;display:block" alt="Chart">'
            )
        else:
            lines.append(
                "<div class='note'>Chart image file was referenced in the log but "
                "could not be found on disk — it may have been moved or deleted.</div>"
            )
    else:
        lines.append(
            "<div class='note'>No chart image is available for this entry — "
            "either it predates automatic image rendering or rendering failed. "
            "The CSV output file is still available below.</div>"
        )

    if generated_sql:
        lines.append("<p><strong>Generated SQL:</strong></p>")
        lines.append(f"<pre>{_esc(generated_sql)}</pre>")

        # Derive query shape from the actual SQL — aggregated if GROUP BY is present.
        if re.search(r"\bGROUP\s+BY\b", generated_sql, re.IGNORECASE):
            shape_note = (
                f"Aggregated (GROUP BY present): each output row is a group summary — "
                f"the right grain for a {chart_type} that compares values across categories."
            )
        else:
            shape_note = (
                f"Row-level (no GROUP BY): each output row is a source record — "
                f"the right grain for a {chart_type} that plots individual data points."
            )
        lines.append(f"<div class='note'><strong>Query shape:</strong> {_esc(shape_note)}</div>")

        assumptions = _extract_query_assumptions(generated_sql)
        if assumptions:
            lines.append("<p><strong>Assumptions:</strong></p><ul>")
            for a in assumptions:
                lines.append(f"<li>{_esc(a)}</li>")
            lines.append("</ul>")

    lines.append(f"<p><strong>Output file:</strong> <code>{_esc(output_file_path)}</code></p>")
    return "\n".join(lines)


def _section_summary(entry: dict, cleaning_context: str = "") -> str:
    """Summary section: quotes the already-computed final_answer (deterministic
    relative to the executed SQL, produced once by represent_final_answer /
    build_visualization) as the primary content, lightly reformatted — instead
    of firing a second independent LLM call that re-synthesizes the same
    numbers from scratch and risks drifting from what the Results table and
    final_answer already say. A short deterministic cleaning-context line is
    prepended when cleaning was performed, so the sequence (clean -> analyze)
    is still visible without inventing new prose.
    """
    final_answer = (entry.get("final_answer") or "").strip()

    parts = ["<h2>Summary</h2>"]
    if cleaning_context:
        parts.append(
            f"<p><strong>Data cleaning performed:</strong> {_esc(cleaning_context)}</p>"
        )
    if final_answer:
        for para in re.split(r"\n\s*\n", final_answer):
            para = para.strip()
            if para:
                parts.append(f"<p>{_esc(para)}</p>")
    else:
        parts.append("<p>No final answer was recorded for this entry.</p>")

    return "\n".join(parts)


# ── Public entry point ─────────────────────────────────────────────────────────

def generate_report(entry: dict) -> str:
    """Build an HTML report from a query_log.jsonl entry dict.
    Returns the path to the written HTML file.
    """
    question = entry.get("user_question") or entry.get("curated_question") or "report"
    # Prefer the cleaned-up question for the human-facing title — user_question
    # still carries any router prefix (e.g. "Visualize: ") verbatim, which reads
    # poorly truncated to 60 chars; curated_question is the same question with
    # only wording cleanup applied (see curate_question), so it's what a reader
    # actually wants to see as the report's title.
    title_question = entry.get("curated_question") or question
    from utils.llm_pick import pick_llm
    sql = entry.get("generated_sql_query", "")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Detect which tables the SQL actually touched.
    try:
        known_tables = _all_table_names()
        touched_tables = _detect_tables_in_sql(sql, known_tables) if sql else []
    except Exception:
        known_tables = []
        touched_tables = []

    # Live row/column metadata per touched table.
    try:
        table_meta = _table_metadata(touched_tables)
    except Exception:
        table_meta = []
    table_columns = {tm["table"]: [c["name"] for c in tm["columns"]] for tm in table_meta}

    # Cleaning history for each touched table.
    try:
        cleaning_map = _cleaning_entries_for_tables(touched_tables)
    except Exception:
        cleaning_map = {t: None for t in touched_tables}

    # Plain-text cleaning context for LLM sections.
    cleaning_context = _cleaning_context_text(cleaning_map)

    llm = pick_llm("cheap")

    intro = _section_introduction(entry, table_meta)
    results = _section_results(entry)
    cleaning = _section_data_cleaning(cleaning_map, sql_query=sql, table_columns=table_columns)
    question_shaping = _section_question_transformations(sql)
    topic = _section_topic_focus(entry, llm, cleaning_context)
    viz = _section_visualization(entry)
    summary = _section_summary(entry, cleaning_context)

    body_parts = [intro, results, cleaning, question_shaping, topic]
    if viz:
        body_parts.append(viz)
    body_parts.append(summary)
    body = "\n\n".join(body_parts)

    title = f"Report: {title_question[:60]}"
    full_html = _html_page(title, body, generated_at)

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{_slug(question)}_{ts_str}.html"
    out_path = _REPORTS_DIR / filename
    out_path.write_text(full_html, encoding="utf-8")
    return str(out_path)


def last_query_log_entry() -> dict | None:
    """Return the most recent entry from query_log.jsonl, or None if the log is empty."""
    if not _QUERY_LOG_PATH.exists():
        return None
    lines = [l for l in _QUERY_LOG_PATH.read_text().splitlines() if l.strip()]
    if not lines:
        return None
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
