"""Generate an HTML slideshow presentation from a query_log.jsonl entry.

Public interface: generate_presentation(entry) -> str (path to HTML file).

Slide structure — every fact traced to real logged data, nothing invented:
  1. Title slide.
  2. "What are we exploring?" — live column-to-category mapping.
  3. The question stated large + real variables involved.
  4. "Uncleaned Data" — real issue categories found.
  5. One slide per real resolved fail-level issue (Issue / Solution format).
     Omitted entirely when no cleaning history exists.
  6. "Cleaned Data" — real before/after row-count comparison.
  7. Visualization — chart image embedded full-slide, reasoning if reasoned.
     Only present for visualize: entries.
  8. Summary — grounded synthesis, same standard as the report.
"""

import base64
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CLEANING_LOG_PATH = _PROJECT_ROOT / "logs" / "cleaning_log.jsonl"
_PRESENTATIONS_DIR = _PROJECT_ROOT / "presentations"


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _slug(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", question.lower())[:40].strip("_")


# ── Database lookups (read-only, app_reader) ───────────────────────────────────

def _all_table_names() -> list:
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
    sql_upper = sql.upper()
    return [t for t in known_tables if re.search(r"\b" + re.escape(t.upper()) + r"\b", sql_upper)]


def _table_metadata(table_names: list) -> list:
    if not table_names:
        return []
    from utils.db import get_app_reader_connection
    conn = get_app_reader_connection()
    meta = []
    try:
        with conn.cursor() as cur:
            for tname in table_names:
                cur.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                    ("public", tname),
                )
                columns = [{"name": row[0], "type": row[1]} for row in cur.fetchall()]
                cur.execute(f'SELECT COUNT(*) FROM "{tname}"')  # noqa: S608 — tname from information_schema
                row_count = cur.fetchone()[0]
                meta.append({"table": tname, "row_count": row_count, "columns": columns})
    finally:
        conn.close()
    return meta


# ── Column categorisation (for slide 2) ───────────────────────────────────────

_TEMPORAL_TYPES = {
    "timestamp without time zone", "timestamp with time zone",
    "date", "time without time zone", "time with time zone",
}
_NUMERIC_TYPES = {
    "integer", "bigint", "smallint", "numeric", "decimal",
    "real", "double precision", "float", "money",
}
_ID_RE = re.compile(r"(_id|_key|index|_code|_num)$", re.IGNORECASE)


def _column_categories(columns: list) -> dict:
    """Group columns into broad human-readable categories for the overview slide."""
    cats: dict[str, list] = {
        "Identifiers": [],
        "Temporal": [],
        "Numeric": [],
        "Text / Categorical": [],
    }
    for col in columns:
        name = col["name"]
        dtype = col["type"].lower()
        if dtype in _TEMPORAL_TYPES:
            cats["Temporal"].append(name)
        elif dtype in _NUMERIC_TYPES:
            if _ID_RE.search(name):
                cats["Identifiers"].append(name)
            else:
                cats["Numeric"].append(name)
        else:
            if _ID_RE.search(name):
                cats["Identifiers"].append(name)
            else:
                cats["Text / Categorical"].append(name)
    return {k: v for k, v in cats.items() if v}


# ── Cleaning log lookup ────────────────────────────────────────────────────────

def _cleaning_entries_for_tables(table_names: list) -> dict:
    result = {t: None for t in table_names}
    if not _CLEANING_LOG_PATH.exists() or not table_names:
        return result
    normalized_lookup = {t.lower().strip(): t for t in table_names}
    for line in _CLEANING_LOG_PATH.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        for file_rec in entry.get("files", []):
            tname = file_rec.get("table_name", "").lower().strip()
            if tname in normalized_lookup:
                result[normalized_lookup[tname]] = (entry, file_rec)
    return result


# ── SQL variable extraction (for slide 3) ─────────────────────────────────────

_SQL_KEYWORDS = frozenset({
    "as", "distinct", "count", "sum", "avg", "min", "max", "from",
    "where", "group", "by", "having", "order", "limit", "and", "or",
    "not", "null", "true", "false", "case", "when", "then", "else",
    "end", "in", "is", "like", "between", "join", "on", "inner",
    "outer", "left", "right", "cross", "full", "union", "all",
    "select", "insert", "update", "delete", "with", "over", "partition",
})


def _extract_sql_variables(sql: str) -> list[str]:
    """Extract column-like identifiers from the SELECT clause."""
    if not sql:
        return []
    m = re.search(r"\bSELECT\b(.+?)\bFROM\b", sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return []
    tokens = {
        tok for tok in re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", m.group(1))
        if tok.lower() not in _SQL_KEYWORDS and len(tok) > 1
    }
    return sorted(tokens)


# ── Issue category label (for slide 4) ────────────────────────────────────────

def _issue_category(issue_text: str) -> str:
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
    return "Other"


# ── LLM summary (slide 8) ──────────────────────────────────────────────────────

def _generate_summary(entry: dict, cleaning_context: str, llm) -> str:
    question = entry.get("user_question") or entry.get("curated_question") or ""
    final_answer = (entry.get("final_answer") or "")[:1000]
    route = entry.get("route_response", "")

    facts = (
        f"Question: {question}\n"
        f"Run type: {route}\n"
        f"Final answer: {final_answer}\n"
    )
    if cleaning_context:
        facts += f"Data cleaning performed: {cleaning_context}\n"
    if entry.get("chart_type"):
        facts += f"Chart type produced: {entry['chart_type']}\n"

    prompt = (
        f"Write a 3-5 sentence summary for a data presentation. "
        f"Synthesize ONLY from the facts listed below — do not introduce any number, "
        f"claim, or interpretation not directly traceable to these facts. "
        f"If cleaning was performed, briefly note why it was necessary for this analysis. "
        f"Write plain prose only — no markdown headers, no bullet points, no formatting symbols.\n\n"
        f"Facts:\n{facts}"
    )
    text = llm.invoke([("human", prompt)]).content
    if isinstance(text, list):
        text = "".join(
            b.get("text", "") if isinstance(b, dict) else str(b)
            for b in text if not (isinstance(b, dict) and b.get("type") == "thinking")
        )
    return text


# ── Slide CSS and JS ───────────────────────────────────────────────────────────

_SLIDE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  background:#1a1a2e;color:#e0e0e0;height:100vh;
  display:flex;flex-direction:column;overflow:hidden}
.slide{display:none;flex:1;padding:56px 80px;flex-direction:column;
  justify-content:center;align-items:flex-start;min-height:0;overflow:auto}
.slide.active{display:flex}
.label{font-size:.78em;text-transform:uppercase;letter-spacing:.14em;
  color:#5a7a9a;margin-bottom:10px}
.title{font-size:2.5em;font-weight:700;color:#fff;line-height:1.2;margin-bottom:16px}
.subtitle{font-size:1.2em;color:#8aa8c8;margin-top:6px}
.question{font-size:1.9em;font-weight:600;color:#7eb8f7;line-height:1.35;
  margin-bottom:22px;max-width:88%}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:.9em;
  background:#16213e;max-width:860px}
th,td{border:1px solid #253a5c;padding:8px 14px;text-align:left;vertical-align:top}
th{background:#1f3460;font-weight:600;color:#90c4ff}
td{color:#b8cce0}
.chip{display:inline-block;padding:3px 10px;border-radius:12px;font-size:.82em;
  margin:2px 3px;background:#1f3460;color:#90caf9}
.issue-box{background:#261616;border-left:4px solid #c62828;
  padding:16px 20px;border-radius:4px;margin:10px 0;max-width:860px}
.solution-box{background:#162616;border-left:4px solid #2e7d32;
  padding:16px 20px;border-radius:4px;margin:10px 0;max-width:860px}
.issue-label{color:#ef9a9a;font-size:.78em;text-transform:uppercase;
  letter-spacing:.1em;font-weight:700;margin-bottom:6px}
.solution-label{color:#a5d6a7;font-size:.78em;text-transform:uppercase;
  letter-spacing:.1em;font-weight:700;margin-bottom:6px}
.issue-text{color:#ffcdd2;font-size:.97em;line-height:1.55}
.solution-text{color:#c8e6c9;font-size:.97em;line-height:1.55}
.ba-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px;
  margin-top:18px;max-width:620px}
.ba-cell{background:#16213e;border:1px solid #253a5c;border-radius:8px;
  padding:22px;text-align:center}
.ba-num{font-size:2.4em;font-weight:700;color:#7eb8f7;margin-bottom:6px}
.ba-desc{font-size:.86em;color:#6a8aaa}
.tag-fail{color:#ef9a9a;font-weight:600}
.tag-warn{color:#ffcc80}
img.chart{max-height:68vh;max-width:100%;border:1px solid #253a5c;
  border-radius:6px;display:block;margin:14px 0}
.summary-text{font-size:1.12em;line-height:1.75;color:#c0d4e8;max-width:800px}
.nav{display:flex;align-items:center;justify-content:space-between;
  padding:14px 80px;background:#0f0f23;border-top:1px solid #253a5c;flex-shrink:0}
.nav button{background:#1f3460;color:#90c4ff;border:1px solid #253a5c;
  padding:8px 22px;border-radius:4px;cursor:pointer;font-size:.88em;transition:background .15s}
.nav button:hover{background:#2a4a8c}
.nav button:disabled{opacity:.3;cursor:default}
#counter{color:#6a8aaa;font-size:.86em}
"""

_SLIDE_JS = """
const slides = document.querySelectorAll('.slide');
let cur = 0;
const counter = document.getElementById('counter');
const prevBtn = document.getElementById('prev');
const nextBtn = document.getElementById('next');

function show(n) {
  slides[cur].classList.remove('active');
  cur = Math.max(0, Math.min(n, slides.length - 1));
  slides[cur].classList.add('active');
  counter.textContent = (cur + 1) + ' / ' + slides.length;
  prevBtn.disabled = (cur === 0);
  nextBtn.disabled = (cur === slides.length - 1);
}

prevBtn.addEventListener('click', () => show(cur - 1));
nextBtn.addEventListener('click', () => show(cur + 1));
document.addEventListener('keydown', e => {
  if (e.key === 'ArrowRight' || e.key === 'ArrowDown') show(cur + 1);
  if (e.key === 'ArrowLeft'  || e.key === 'ArrowUp')   show(cur - 1);
});

show(0);
"""


# ── Slide builders ─────────────────────────────────────────────────────────────

def _slide_title(entry: dict, table_meta: list, generated_at: str) -> str:
    question = entry.get("user_question") or entry.get("curated_question") or "Data Analysis"
    datasets = ", ".join(tm["table"] for tm in table_meta) or "unknown dataset"
    return (
        f'<div class="slide">'
        f'<div class="label">Data Analysis Presentation</div>'
        f'<h1 class="title">{_esc(question[:120])}</h1>'
        f'<p class="subtitle">Dataset: {_esc(datasets)}</p>'
        f'<p class="subtitle" style="margin-top:8px;color:#3a5a7a">'
        f'Generated {_esc(generated_at)}</p>'
        f'</div>'
    )


def _slide_what_are_we_exploring(table_meta: list) -> str:
    parts = [
        '<div class="slide">',
        '<div class="label">Dataset Overview</div>',
        '<h2 class="title" style="font-size:2em">What are we exploring?</h2>',
    ]
    if table_meta:
        for tm in table_meta:
            cats = _column_categories(tm["columns"])
            parts.append(
                f'<p style="color:#8aa8c8;margin:8px 0">'
                f'<strong style="color:#c8dff0">{_esc(tm["table"])}</strong>'
                f' — {tm["row_count"]:,} rows, {len(tm["columns"])} columns</p>'
            )
            if cats:
                parts.append(
                    '<table style="margin-top:10px">'
                    '<thead><tr><th>Category</th><th>Columns</th></tr></thead><tbody>'
                )
                for cat, cols in cats.items():
                    chips = "".join(f'<span class="chip">{_esc(c)}</span>' for c in cols)
                    parts.append(f'<tr><td>{_esc(cat)}</td><td>{chips}</td></tr>')
                parts.append('</tbody></table>')
    else:
        parts.append('<p style="color:#6a8aaa">No specific tables identified from the query.</p>')
    parts.append('</div>')
    return "\n".join(parts)


def _slide_question(entry: dict) -> str:
    question = entry.get("user_question") or entry.get("curated_question") or ""
    sql = entry.get("generated_sql_query", "")
    variables = _extract_sql_variables(sql)
    parts = [
        '<div class="slide">',
        '<div class="label">The Question</div>',
        f'<h2 class="question">{_esc(question)}</h2>',
    ]
    if variables:
        chips = "".join(f'<span class="chip">{_esc(v)}</span>' for v in variables[:14])
        parts.append(
            f'<div><div class="label" style="margin-top:18px">Variables involved</div>'
            f'{chips}</div>'
        )
    parts.append('</div>')
    return "\n".join(parts)


def _slide_uncleaned_data(all_issues: list) -> str:
    fc = sum(1 for i in all_issues if i.get("severity") == "fail")
    wc = sum(1 for i in all_issues if i.get("severity") == "warn")

    # Collect unique category+severity pairs in encountered order.
    seen: list[tuple] = []
    seen_keys: set = set()
    for iss in all_issues:
        cat = _issue_category(iss.get("issue", ""))
        sev = iss.get("severity", "")
        key = (cat, sev)
        if key not in seen_keys:
            seen_keys.add(key)
            seen.append(key)

    parts = [
        '<div class="slide">',
        '<div class="label">Data Quality Audit</div>',
        '<h2 class="title" style="font-size:2em">Uncleaned Data</h2>',
        f'<p style="color:#8aa8c8;margin-bottom:16px">'
        f'{len(all_issues)} issue{"s" if len(all_issues) != 1 else ""} detected — '
        f'<span class="tag-fail">{fc} critical</span>, '
        f'<span class="tag-warn">{wc} advisory</span></p>',
        '<table><thead><tr><th>Issue Category</th><th>Severity</th></tr></thead><tbody>',
    ]
    for cat, sev in seen:
        cls = "tag-fail" if sev == "fail" else "tag-warn"
        parts.append(
            f'<tr><td>{_esc(cat)}</td>'
            f'<td><span class="{cls}">{_esc(sev)}</span></td></tr>'
        )
    parts.append('</tbody></table>')
    parts.append('</div>')
    return "\n".join(parts)


def _slides_issue_solution(tname: str, file_rec: dict) -> list[str]:
    """One slide per resolved fail-level issue."""
    slides = []
    fail_issues = file_rec.get("fail_issues", [])
    resolved_set = set(file_rec.get("issues_resolved", []))

    for fi in fail_issues:
        issue_text = fi.get("issue", "")
        if issue_text not in resolved_set:
            continue

        reasoning = fi.get("reasoning_comments", [])
        if reasoning:
            solution = " ".join(
                re.sub(r"^#\s*", "", c) for c in reasoning
            ).strip()
        else:
            solution = "The issue was resolved — see full report for fix details."

        slides.append(
            f'<div class="slide">'
            f'<div class="label">Data Cleaning — {_esc(tname)}</div>'
            f'<div class="issue-box">'
            f'<div class="issue-label">Issue</div>'
            f'<div class="issue-text">{_esc(issue_text)}</div>'
            f'</div>'
            f'<div class="solution-box">'
            f'<div class="solution-label">Solution</div>'
            f'<div class="solution-text">{_esc(solution[:500])}</div>'
            f'</div>'
            f'</div>'
        )
    return slides


def _slide_cleaned_data(tname: str, file_rec: dict) -> str:
    rb = file_rec.get("row_count_before")
    ra = file_rec.get("row_count_after")
    resolved = file_rec.get("issues_resolved", [])
    unresolved = file_rec.get("issues_still_unresolved", [])

    parts = [
        '<div class="slide">',
        f'<div class="label">After Cleaning — {_esc(tname)}</div>',
        '<h2 class="title" style="font-size:2em">Cleaned Data</h2>',
    ]
    if rb is not None and ra is not None:
        parts.append(
            f'<div class="ba-grid">'
            f'<div class="ba-cell"><div class="ba-num">{rb:,}</div>'
            f'<div class="ba-desc">Rows before cleaning</div></div>'
            f'<div class="ba-cell"><div class="ba-num">{ra:,}</div>'
            f'<div class="ba-desc">Rows after cleaning</div></div>'
            f'</div>'
        )
    parts.append(
        f'<p style="margin-top:18px;color:#8aa8c8">'
        f'{len(resolved)} issue{"s" if len(resolved) != 1 else ""} resolved, '
        f'{len(unresolved)} unresolved.</p>'
    )
    parts.append('</div>')
    return "\n".join(parts)


def _slide_visualization(entry: dict) -> str:
    chart_type = entry.get("chart_type", "")
    chart_image_path = entry.get("chart_image_path", "")
    chart_type_source = entry.get("chart_type_source", "")
    chart_type_reasoning = entry.get("chart_type_reasoning", "")

    parts = [
        '<div class="slide">',
        f'<div class="label">Visualization — {_esc(chart_type)}</div>',
    ]

    if chart_image_path:
        img_path = Path(chart_image_path)
        if img_path.exists():
            b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
            parts.append(f'<img class="chart" src="data:image/png;base64,{b64}" alt="Chart">')
        else:
            parts.append(
                f'<p style="color:#6a8aaa">Chart image not found at: '
                f'{_esc(chart_image_path)}</p>'
            )
    else:
        parts.append('<p style="color:#6a8aaa">No chart image available for this entry.</p>')

    if chart_type_source == "reasoned" and chart_type_reasoning:
        parts.append(
            f'<p style="color:#8aa8c8;margin-top:14px">'
            f'<strong style="color:#c8dff0">Why this chart type:</strong> '
            f'{_esc(chart_type_reasoning)}</p>'
        )

    parts.append('</div>')
    return "\n".join(parts)


def _slide_summary(summary_text: str) -> str:
    return (
        f'<div class="slide">'
        f'<div class="label">Summary</div>'
        f'<h2 class="title" style="font-size:2em">Key Takeaways</h2>'
        f'<p class="summary-text">{_esc(summary_text)}</p>'
        f'</div>'
    )


# ── Full HTML assembly ─────────────────────────────────────────────────────────

def _assemble_html(slides: list[str], title: str) -> str:
    slide_html = "\n".join(slides)
    return (
        f"<!DOCTYPE html>\n<html lang='en'>\n<head>\n"
        f"<meta charset='UTF-8'>\n"
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_SLIDE_CSS}</style>\n"
        f"</head>\n<body>\n"
        f"{slide_html}\n"
        f'<nav class="nav">\n'
        f'  <button id="prev">&#8592; Prev</button>\n'
        f'  <span id="counter"></span>\n'
        f'  <button id="next">Next &#8594;</button>\n'
        f"</nav>\n"
        f"<script>{_SLIDE_JS}</script>\n"
        f"</body>\n</html>\n"
    )


# ── Public entry point ─────────────────────────────────────────────────────────

def generate_presentation(entry: dict) -> str:
    """Build an HTML slideshow from a query_log.jsonl entry dict.
    Returns the path to the written HTML file.
    """
    from utils.llm_pick import pick_llm

    question = entry.get("user_question") or entry.get("curated_question") or "presentation"
    sql = entry.get("generated_sql_query", "")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Detect touched tables.
    try:
        known_tables = _all_table_names()
        touched_tables = _detect_tables_in_sql(sql, known_tables) if sql else []
    except Exception:
        known_tables = []
        touched_tables = []

    # Live table metadata.
    try:
        table_meta = _table_metadata(touched_tables)
    except Exception:
        table_meta = []

    # Cleaning history.
    try:
        cleaning_map = _cleaning_entries_for_tables(touched_tables)
    except Exception:
        cleaning_map = {t: None for t in touched_tables}

    # Build plain-text cleaning context for the summary LLM call.
    cleaning_ctx_parts = []
    for tname, rec in cleaning_map.items():
        if rec is None:
            continue
        entry_meta, file_rec = rec
        issues = file_rec.get("issues_found", [])
        resolved = file_rec.get("issues_resolved", [])
        unresolved = file_rec.get("issues_still_unresolved", [])
        fc = sum(1 for i in issues if i.get("severity") == "fail")
        wc = sum(1 for i in issues if i.get("severity") == "warn")
        cleaning_ctx_parts.append(
            f"'{tname}': {len(issues)} issues ({fc} critical, {wc} advisory), "
            f"{len(resolved)} resolved, {len(unresolved)} unresolved"
        )
    cleaning_context = " | ".join(cleaning_ctx_parts)

    llm = pick_llm("cheap")
    summary_text = _generate_summary(entry, cleaning_context, llm)

    # ── Assemble slides in order ───────────────────────────────────────────────
    slides: list[str] = []

    # 1. Title
    slides.append(_slide_title(entry, table_meta, generated_at))

    # 2. What are we exploring?
    slides.append(_slide_what_are_we_exploring(table_meta))

    # 3. The question + variables
    slides.append(_slide_question(entry))

    # 4-6. Cleaning slides (only when there is real cleaning history)
    has_history = any(rec is not None for rec in cleaning_map.values())
    if has_history:
        # Collect all issues across tables for the overview slide.
        all_issues = []
        for tname, rec in cleaning_map.items():
            if rec is None:
                continue
            entry_meta, file_rec = rec
            all_issues.extend(file_rec.get("issues_found", []))

        # 4. Uncleaned Data overview
        if all_issues:
            slides.append(_slide_uncleaned_data(all_issues))

        # 5. One slide per resolved fail issue
        for tname, rec in cleaning_map.items():
            if rec is None:
                continue
            entry_meta, file_rec = rec
            slides.extend(_slides_issue_solution(tname, file_rec))

        # 6. Cleaned Data (before/after)
        for tname, rec in cleaning_map.items():
            if rec is None:
                continue
            entry_meta, file_rec = rec
            slides.append(_slide_cleaned_data(tname, file_rec))

    # 7. Visualization (visualize: entries only)
    if entry.get("chart_type") and entry.get("output_file_path"):
        slides.append(_slide_visualization(entry))

    # 8. Summary
    slides.append(_slide_summary(summary_text))

    # ── Write to disk ──────────────────────────────────────────────────────────
    _PRESENTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{_slug(question)}_{ts_str}.html"
    out_path = _PRESENTATIONS_DIR / filename
    out_path.write_text(_assemble_html(slides, question[:60]), encoding="utf-8")
    return str(out_path)
