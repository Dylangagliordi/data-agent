"""Generate an HTML report from a query_log.jsonl entry.

Public interface: generate_report(entry) -> str (path to the written HTML file).

Every section is built ONLY from real, traceable data — never invented:
1. Introduction — dataset name, live row/column counts, the actual question asked,
   and which cleaning issues (if any) were found for the relevant table(s).
2. Data Cleaning — pulled directly from cleaning_log.jsonl for the touched tables;
   if no cleaning history exists, states that honestly.
3. Topic Focus — one pick_llm("cheap") call grounded in the actual question + result.
4. Visualization — ONLY for visualize: entries with chart_type/output_file_path set.
5. Summary — one pick_llm("cheap") call synthesizing only from assembled facts.
"""

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

    lines = _CLEANING_LOG_PATH.read_text().splitlines()
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        for file_rec in entry.get("files", []):
            tname = file_rec.get("table_name", "")
            if tname in result:
                result[tname] = (entry, file_rec)
    return result


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


def _section_data_cleaning(cleaning_map: dict) -> str:
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

        parts.append(
            f"<p><strong>Trigger:</strong> {_esc(trigger)} &nbsp;|&nbsp; "
            f"<strong>Cleaned at:</strong> {_esc(ts)} &nbsp;|&nbsp; "
            f"<strong>Status:</strong> {_esc(status)}</p>"
        )

        # Before / after row counts.
        if rb is not None and ra is not None:
            parts.append(
                "<table><thead><tr><th>Before cleaning</th><th>After cleaning</th><th>Note</th></tr></thead><tbody>"
                f"<tr><td>{rb:,} rows</td><td>{ra:,} rows</td>"
                f"<td>{'<span class=\"bad\">Row loss flagged (&ge;20%)</span>' if row_loss else 'Within acceptable range'}</td>"
                f"</tr></tbody></table>"
            )

        # Issues found.
        issues_found = file_rec.get("issues_found", [])
        if issues_found:
            parts.append("<p><strong>Issues found:</strong></p><ul>")
            for iss in issues_found:
                sev = iss.get("severity", "")
                css = "bad" if sev == "fail" else ""
                parts.append(f"<li><span class='{css}'>[{_esc(sev)}]</span> {_esc(iss.get('issue', ''))}</li>")
            parts.append("</ul>")

        # Resolved / unresolved.
        resolved = file_rec.get("issues_resolved", [])
        unresolved = file_rec.get("issues_still_unresolved", [])
        if resolved:
            parts.append("<p><strong>Resolved:</strong></p><ul>")
            for i in resolved:
                parts.append(f"<li class='ok'>&#x2713; {_esc(i)}</li>")
            parts.append("</ul>")
        if unresolved:
            parts.append("<p><strong>Still unresolved:</strong></p><ul>")
            for i in unresolved:
                parts.append(f"<li class='bad'>&#x2717; {_esc(i)}</li>")
            parts.append("</ul>")

        # Reasoning comments from generated fix code.
        all_comments = []
        for fi in file_rec.get("fail_issues", []):
            all_comments.extend(fi.get("reasoning_comments", []))
        wb = file_rec.get("warn_batch") or {}
        all_comments.extend(wb.get("reasoning_comments", []))

        if all_comments:
            parts.append("<p><strong>Reasoning from generated fix code:</strong></p>")
            parts.append("<pre>" + _esc("\n".join(all_comments)) + "</pre>")

    return "\n".join(parts)


def _section_topic_focus(entry: dict, llm) -> str:
    question = entry.get("user_question") or entry.get("curated_question") or ""
    result_snippet = (entry.get("sql_query_execution_result") or "")[:500]
    final_answer = (entry.get("final_answer") or "")[:800]

    prompt = (
        f"A data analyst asked this question: \"{question}\"\n\n"
        f"The query returned: {result_snippet}\n\n"
        f"The final answer given was: {final_answer}\n\n"
        f"In 2-4 sentences, explain why this is a meaningful question to ask of this dataset. "
        f"Be specific and grounded in what the data actually showed — do not invent "
        f"business context or mention anything not directly present in the question and result above. "
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

    if not chart_type or not output_file_path:
        return ""

    lines = [
        "<h2>Visualization</h2>",
        f"<p><strong>Chart type:</strong> {_esc(chart_type)}</p>",
        f"<p><strong>Selection method:</strong> {_esc(chart_type_source)}</p>",
    ]
    if chart_type_reasoning:
        lines.append(f"<p><strong>Reasoning:</strong> {_esc(chart_type_reasoning)}</p>")
    lines.append(f"<p><strong>Output file:</strong> <code>{_esc(output_file_path)}</code></p>")
    return "\n".join(lines)


def _section_summary(intro_html: str, cleaning_html: str, topic_html: str,
                     viz_html: str, entry: dict, llm) -> str:
    question = entry.get("user_question") or entry.get("curated_question") or ""
    final_answer = (entry.get("final_answer") or "")[:1000]
    route = entry.get("route_response", "")

    facts = (
        f"Question: {question}\n"
        f"Run type: {route}\n"
        f"Final answer: {final_answer}\n"
    )
    if entry.get("chart_type"):
        facts += f"Chart type produced: {entry['chart_type']}\n"
    if entry.get("output_file_path"):
        facts += f"Output file: {entry['output_file_path']}\n"

    prompt = (
        f"Write a 3-5 sentence summary for a data report. "
        f"Synthesize ONLY from the facts listed below — do not introduce any number, "
        f"claim, or interpretation not directly traceable to these facts. "
        f"Write plain prose only — no markdown headers, no bullet points, no formatting symbols.\n\n"
        f"Facts:\n{facts}"
    )
    summary_text = llm.invoke([("human", prompt)]).content
    if isinstance(summary_text, list):
        summary_text = "".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in summary_text if not (isinstance(b, dict) and b.get("type") == "thinking")
        )

    return f"<h2>Summary</h2>\n<p>{_esc(summary_text)}</p>"


# ── Public entry point ─────────────────────────────────────────────────────────

def generate_report(entry: dict) -> str:
    """Build an HTML report from a query_log.jsonl entry dict.
    Returns the path to the written HTML file.
    """
    from utils.llm_pick import pick_llm

    question = entry.get("user_question") or entry.get("curated_question") or "report"
    sql = entry.get("generated_sql_query", "")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Detect which tables the SQL actually touched.
    try:
        known_tables = _all_table_names()
        touched_tables = _detect_tables_in_sql(sql, known_tables) if sql else []
    except Exception as e:
        known_tables = []
        touched_tables = []

    # Live row/column metadata per touched table.
    try:
        table_meta = _table_metadata(touched_tables)
    except Exception:
        table_meta = []

    # Cleaning history for each touched table.
    try:
        cleaning_map = _cleaning_entries_for_tables(touched_tables)
    except Exception:
        cleaning_map = {t: None for t in touched_tables}

    llm = pick_llm("cheap")

    intro = _section_introduction(entry, table_meta)
    cleaning = _section_data_cleaning(cleaning_map)
    topic = _section_topic_focus(entry, llm)
    viz = _section_visualization(entry)
    summary = _section_summary(intro, cleaning, topic, viz, entry, llm)

    body_parts = [intro, cleaning, topic]
    if viz:
        body_parts.append(viz)
    body_parts.append(summary)
    body = "\n\n".join(body_parts)

    title = f"Report: {question[:60]}"
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
