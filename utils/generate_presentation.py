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
  6.5. "How we shaped the data for this answer" — deterministic breakdown of
       the SQL transformations unique to answering THIS question (computed
       metrics, grouping, minimum sample size, ranking/limiting, scope
       filters) — separate from the general cleaning above. Present for both
       sql_analyst and visualize: entries whenever the executed SQL has real
       shaping to show; skipped when it's a plain passthrough query.
  7. "Making sense of the categories" — plain-English glossary of the real
     category values (e.g. industry names) that appear in the result, when
     the result has a resolvable category column. Only present for
     visualize: entries; omitted when no such column exists.
  8. Visualization — chart image embedded full-slide, reasoning if reasoned.
     Only present for visualize: entries.
  9. Scatter, sized by volume — a second view of the same result plotted on
     two numeric axes with point size encoding a count-like column (e.g. how
     many job postings back each category's numbers). Only built when the
     real result actually has a category column, 2+ numeric measure columns,
     and a count-like column — never invented when the shape doesn't fit.
  10. Summary — grounded synthesis, written in a plain, conversational voice
      that explains the actual reasoning behind each judgment call, not just
      the raw facts.
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

_COUNT_COL_RE = re.compile(
    r"^(?:n|count|num\w*|sample_size|total_count|\w+_count)$", re.IGNORECASE
)
_MAX_GLOSSARY_TERMS = 12


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


# ── Real-result parsing (shared by the glossary + scatter slides) ─────────────

def _parse_result_rows(result_str: str) -> tuple[list, bool]:
    """Parse execute_sql's {"columns", "rows", "truncated"} JSON payload into a
    list of row dicts. Returns ([], False) for an error string or unparseable
    input. Deliberately its own copy (same shape as generate_report.py's and
    agents/sql_analyst.py's own parsers) rather than a cross-module import, to
    avoid coupling three independently-testable files together.
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


def _to_float(val):
    """Safely coerce any value to float; return None if not possible."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _humanize_col(col: str) -> str:
    """Convert a SQL alias like 'avg_salary_k' -> 'Avg Salary K'."""
    return col.replace("_", " ").title()


def _resolve_category_values(entry: dict) -> "tuple[str, list[str]] | None":
    """Deterministically pick the real category column out of the executed
    query's actual result (first non-numeric column, by real column order —
    no LLM involved in this step) and return its distinct values in the order
    they first appear. Returns None when the result has no non-numeric column
    at all (nothing to build a glossary of).
    """
    rows, truncated = _parse_result_rows(entry.get("sql_query_execution_result", ""))
    if not rows:
        return None
    cols = list(rows[0].keys())
    numeric_cols = {c for c in cols if any(_to_float(r.get(c)) is not None for r in rows)}
    non_numeric_cols = [c for c in cols if c not in numeric_cols]
    if not non_numeric_cols:
        return None
    cat_col = non_numeric_cols[0]
    seen: set = set()
    values: list = []
    for r in rows:
        v = r.get(cat_col)
        if v is None:
            continue
        v = str(v)
        if v not in seen:
            seen.add(v)
            values.append(v)
    if not values:
        return None
    return cat_col, values[:_MAX_GLOSSARY_TERMS]


def _resolve_scatter_columns(entry: dict) -> "dict | None":
    """Deterministically resolve category/x/y/count columns for a second,
    volume-aware scatter view of the SAME already-executed result — never a
    fresh query, never invented data. Returns None when the real result
    doesn't have the right shape: needs one non-numeric category column, a
    real count-like column (job_count, n, num_*, etc.) to size points by, and
    at least two OTHER numeric measure columns to plot on the two axes.
    """
    rows, truncated = _parse_result_rows(entry.get("sql_query_execution_result", ""))
    if not rows or truncated:
        return None
    cols = list(rows[0].keys())
    numeric_cols = [c for c in cols if any(_to_float(r.get(c)) is not None for r in rows)]
    non_numeric_cols = [c for c in cols if c not in numeric_cols]
    if not non_numeric_cols:
        return None
    category_col = non_numeric_cols[0]
    count_col = next((c for c in numeric_cols if _COUNT_COL_RE.match(c)), None)
    if count_col is None:
        return None
    measure_cols = [c for c in numeric_cols if c != count_col]
    if len(measure_cols) < 2:
        return None
    return {
        "rows": rows,
        "category_col": category_col,
        "x_col": measure_cols[0],
        "y_col": measure_cols[1],
        "count_col": count_col,
    }


def _render_scatter_chart_b64(resolved: dict) -> "str | None":
    """Render the volume-aware scatter chart in-memory; return base64 PNG
    bytes, or None if rendering fails for any reason (never blocks the rest
    of the presentation).
    """
    try:
        import io as _io

        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        rows = resolved["rows"]
        cat_col, x_col = resolved["category_col"], resolved["x_col"]
        y_col, count_col = resolved["y_col"], resolved["count_col"]

        xs, ys, sizes, labels = [], [], [], []
        for r in rows:
            x, y = _to_float(r.get(x_col)), _to_float(r.get(y_col))
            if x is None or y is None:
                continue
            n = _to_float(r.get(count_col)) or 0
            xs.append(x)
            ys.append(y)
            sizes.append(n * 6 + 40)
            labels.append(str(r.get(cat_col)))
        if not xs:
            return None

        fig = Figure(figsize=(9.5, 6.2))
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        ax.scatter(xs, ys, s=sizes, alpha=0.75, edgecolors="#16213e", linewidths=0.7, zorder=3)
        for x, y, label in zip(xs, ys, labels):
            ax.annotate(label, (x, y), textcoords="offset points", xytext=(6, 4), fontsize=8)
        ax.set_xlabel(_humanize_col(x_col))
        ax.set_ylabel(_humanize_col(y_col))
        ax.set_title(
            f"{_humanize_col(y_col)} vs. {_humanize_col(x_col)}\n"
            f"(point size = {_humanize_col(count_col)})",
            fontsize=11,
        )
        ax.grid(True, alpha=0.25, zorder=0)
        fig.tight_layout()

        buf = _io.BytesIO()
        fig.savefig(buf, format="png", dpi=150)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


# ── LLM glossary (slide 7 — plain-English category meanings) ──────────────────

def _generate_glossary(category_label: str, values: list, llm) -> dict:
    """One LLM call explaining what each real category value generally MEANS
    in plain English (e.g. what kind of companies 'Staffing & Outsourcing'
    covers) — this is general background knowledge the model already has,
    not a claim derived from the dataset, and is presented to the user framed
    that way. Never invents a number or a dataset-specific fact. Returns {}
    (glossary slide is simply skipped) on any failure or malformed response.
    """
    if not values:
        return {}
    terms_block = "\n".join(f"- {v}" for v in values)
    prompt = (
        f"Someone is looking at a chart grouped by \"{category_label}\" and the category "
        f"labels below aren't self-explanatory. For each one, explain in one short, plain, "
        f"conversational sentence what that label generally refers to in everyday terms — "
        f"this is general background knowledge, not something you're deriving from any "
        f"dataset, so don't state or imply any number, statistic, or dataset-specific fact.\n\n"
        f"Labels:\n{terms_block}\n\n"
        f"Respond with exactly one line per label, in this exact format, same order, "
        f"no numbering, no extra commentary:\n"
        f"<label> :: <one-sentence plain-English explanation>"
    )
    try:
        text = llm.invoke([("human", prompt)]).content
        if isinstance(text, list):
            text = "".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in text if not (isinstance(b, dict) and b.get("type") == "thinking")
            )
        glossary = {}
        for line in text.splitlines():
            if "::" not in line:
                continue
            term, _, explanation = line.partition("::")
            term = term.strip().lstrip("-").strip()
            explanation = explanation.strip()
            if term and explanation:
                glossary[term] = explanation
        return glossary
    except Exception:
        return {}


# ── LLM cleaning-narrative simplification (issue/solution slides) ─────────────

def _simplify_cleaning_narratives(items: list, llm) -> "list | None":
    """One batched LLM call that rewrites every cleaning fix's technical,
    code-comment-style note into a short, plain, conversational explanation —
    same underlying facts, human-sounding delivery. `items` is a list of
    (issue_text, raw_solution_text) tuples; returns a list of rewritten
    solution strings in the same order, or None on any failure/shape
    mismatch (callers must fall back to the original raw text — never drop
    or fabricate a reason).
    """
    if not items:
        return []
    blocks = [
        f"Issue: {issue}\nOriginal fix note: {solution}"
        for issue, solution in items
    ]
    prompt = (
        f"Below are {len(items)} data-cleaning issues and the technical, code-comment-style "
        f"notes explaining how each was fixed. Rewrite ONLY the fix explanation for each one "
        f"in plain, conversational language — like a data analyst talking a colleague through "
        f"their own reasoning out loud, not writing code comments. Keep each to 1-3 short "
        f"sentences. Do not invent any new fact or drop the real reason the data was treated "
        f"the way it was.\n\n"
        f"Respond with exactly {len(items)} rewritten explanations, separated by a line "
        f"containing only ---, in the same order, with no numbering or extra commentary.\n\n"
        + "\n\n".join(blocks)
    )
    try:
        text = llm.invoke([("human", prompt)]).content
        if isinstance(text, list):
            text = "".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in text if not (isinstance(b, dict) and b.get("type") == "thinking")
            )
        parts = [p.strip() for p in text.split("---")]
        parts = [p for p in parts if p]
        if len(parts) != len(items):
            return None
        return parts
    except Exception:
        return None


# ── LLM summary (slide 10) ─────────────────────────────────────────────────────

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
        f"You're a data analyst walking a colleague through what you found and how you got "
        f"there — talk them through it out loud, plainly, like you're explaining your own "
        f"reasoning, not writing a formal report. In 4-6 sentences: say what you found in "
        f"everyday words, AND call out any judgment calls baked into the 'Final answer' "
        f"facts below (e.g. a minimum sample size, excluded rows, why cleaning was needed "
        f"first) and briefly say WHY you made that call, not just that you made it. "
        f"Synthesize ONLY from the facts listed below — never introduce a number, claim, or "
        f"interpretation not directly traceable to them. "
        f"Write plain prose only — no markdown headers, no bullet points, no formatting "
        f"symbols, no jargon you wouldn't actually say out loud.\n\n"
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


def _slides_issue_solution(tname: str, file_rec: dict, llm=None) -> list[str]:
    """One slide per resolved fail-level issue, plus one combined slide per
    resolved fail-level BATCH (Spec 4 — a group of 2+ issues that shared an
    identical, mechanically-verified treatment and got one combined fix),
    listing every issue the batch covered together rather than repeating one
    near-identical slide per column.

    Solution text is rewritten into plain, conversational language via ONE
    batched LLM call (`llm`) covering every issue/batch on this table at
    once — not one call per slide. Falls back to the original raw
    code-comment-style text (unchanged behavior) whenever `llm` is None, the
    call fails, or the response doesn't map 1:1 back onto the real issues."""
    fail_issues = file_rec.get("fail_issues", [])
    resolved_set = set(file_rec.get("issues_resolved", []))

    singleton_entries = []
    for fi in fail_issues:
        issue_text = fi.get("issue", "")
        if issue_text not in resolved_set:
            continue
        reasoning = fi.get("reasoning_comments", [])
        if reasoning:
            solution = " ".join(re.sub(r"^#\s*", "", c) for c in reasoning).strip()
        else:
            solution = "The issue was resolved — see full report for fix details."
        singleton_entries.append({"issue_text": issue_text, "solution": solution[:500]})

    batch_entries = []
    for batch in file_rec.get("fail_batches", []):
        if batch.get("status") != "resolved":
            continue
        batch_issues = batch.get("issues", [])
        if not batch_issues:
            continue
        reasoning = batch.get("reasoning_comments", [])
        if reasoning:
            solution = " ".join(re.sub(r"^#\s*", "", c) for c in reasoning).strip()
        else:
            solution = "The issue was resolved — see full report for fix details."
        issue_text = (
            f"{len(batch_issues)} columns with the identical issue: " + "; ".join(batch_issues)
        )
        batch_entries.append({
            "batch_issues": batch_issues,
            "issue_text": issue_text[:500],
            "solution": solution[:500],
        })

    # One batched simplification call covering every real solution text on
    # this table, in a fixed order — never one LLM call per slide.
    all_solutions = [e["solution"] for e in singleton_entries] + [e["solution"] for e in batch_entries]
    simplified = None
    if llm is not None:
        pairs = (
            [(e["issue_text"], e["solution"]) for e in singleton_entries]
            + [(e["issue_text"], e["solution"]) for e in batch_entries]
        )
        simplified = _simplify_cleaning_narratives(pairs, llm)
    if simplified is not None and len(simplified) == len(all_solutions):
        cursor = 0
        for e in singleton_entries:
            e["display_solution"] = simplified[cursor]
            cursor += 1
        for e in batch_entries:
            e["display_solution"] = simplified[cursor]
            cursor += 1
    else:
        for e in singleton_entries:
            e["display_solution"] = e["solution"]
        for e in batch_entries:
            e["display_solution"] = e["solution"]

    slides = []
    for e in singleton_entries:
        slides.append(
            f'<div class="slide">'
            f'<div class="label">Data Cleaning — {_esc(tname)}</div>'
            f'<div class="issue-box">'
            f'<div class="issue-label">Issue</div>'
            f'<div class="issue-text">{_esc(e["issue_text"])}</div>'
            f'</div>'
            f'<div class="solution-box">'
            f'<div class="solution-label">Solution</div>'
            f'<div class="solution-text">{_esc(e["display_solution"][:600])}</div>'
            f'</div>'
            f'</div>'
        )
    for e in batch_entries:
        slides.append(
            f'<div class="slide">'
            f'<div class="label">Data Cleaning — {_esc(tname)}</div>'
            f'<div class="issue-box">'
            f'<div class="issue-label">Issue (batched, {len(e["batch_issues"])} columns)</div>'
            f'<div class="issue-text">{_esc(e["issue_text"])}</div>'
            f'</div>'
            f'<div class="solution-box">'
            f'<div class="solution-label">Solution</div>'
            f'<div class="solution-text">{_esc(e["display_solution"][:600])}</div>'
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


def _slide_question_shaping(sql_query: str) -> "str | None":
    """One slide showing the deterministic, question-specific SQL shaping —
    metrics computed, grouping, minimum sample size, ranking/limiting, and
    scope filters — separate from the general cleaning slides above. Returns
    None (slide skipped) when the query has no such shaping to show, rather
    than rendering an empty slide.
    """
    from utils.sql_transform_extraction import (
        extract_question_transformations,
        has_any_transformation,
    )

    if not sql_query:
        return None
    t = extract_question_transformations(sql_query)
    if not has_any_transformation(t):
        return None

    parts = [
        '<div class="slide">',
        '<div class="label">Question-Specific Shaping</div>',
        '<h2 class="title" style="font-size:1.9em">How we shaped the data for this answer</h2>',
        '<p style="color:#8aa8c8;margin-bottom:14px;max-width:800px">'
        'Separate from the general cleaning above — this is the transformation '
        'work specific to this question and its chart.</p>',
    ]

    if t["scope_filters"] or t["cte_steps"]:
        if t["cte_steps"]:
            chips = "".join(
                f'<span class="chip">{_esc(s)}</span>' for s in t["cte_steps"]
            )
            parts.append(
                f'<p style="margin-top:6px"><strong style="color:#c8dff0">'
                f'Built in {len(t["cte_steps"])} step(s):</strong> {chips}</p>'
            )
        for f in t["scope_filters"]:
            parts.append(f'<p style="color:#c0d4e8;margin-top:8px">&#9656; {_esc(f)}</p>')

    if t["computed_columns"]:
        parts.append(
            '<table style="margin-top:14px"><thead><tr><th>Metric</th>'
            '<th>How it was computed</th></tr></thead><tbody>'
        )
        for c in t["computed_columns"]:
            parts.append(
                f'<tr><td>{_esc(_humanize_col(c["alias"]))}</td>'
                f'<td>{_esc(c["expression"])}</td></tr>'
            )
        parts.append("</tbody></table>")

    if t["grouping_columns"]:
        for group in t["grouping_columns"]:
            cols_fmt = ", ".join(_esc(c) for c in group)
            parts.append(
                f'<p style="color:#c0d4e8;margin-top:10px"><strong style="color:#c8dff0">'
                f'Grouped by:</strong> {cols_fmt}</p>'
            )

    if t["having_threshold"] is not None:
        parts.append(
            f'<p style="color:#c0d4e8;margin-top:6px"><strong style="color:#c8dff0">'
            f'Minimum sample size:</strong> groups with fewer than '
            f'{t["having_threshold"]} rows were excluded.</p>'
        )

    if t["ranking_stages"]:
        stage_lines = []
        for stage in t["ranking_stages"]:
            if stage["limit"] is not None:
                stage_lines.append(
                    f'ranked by {_esc(stage["order_by"])}, top {stage["limit"]} kept'
                )
            else:
                stage_lines.append(f'ordered by {_esc(stage["order_by"])}')
        parts.append(
            f'<p style="color:#c0d4e8;margin-top:6px"><strong style="color:#c8dff0">'
            f'Ranking:</strong> {" &rarr; ".join(stage_lines)}</p>'
        )

    parts.append("</div>")
    return "\n".join(parts)


def _slide_glossary(category_label: str, glossary: dict) -> str:
    parts = [
        '<div class="slide">',
        '<div class="label">Making Sense Of The Categories</div>',
        '<h2 class="title" style="font-size:2em">What do these groups mean?</h2>',
        f'<p style="color:#8aa8c8;margin-bottom:16px">'
        f'A quick plain-English explanation of each "{_esc(_humanize_col(category_label))}" '
        f'value shown next — general background, not something derived from this data.</p>',
        '<table><thead><tr><th>Category</th><th>What it generally means</th></tr></thead><tbody>',
    ]
    for term, explanation in glossary.items():
        parts.append(f'<tr><td>{_esc(term)}</td><td>{_esc(explanation)}</td></tr>')
    parts.append('</tbody></table>')
    parts.append('</div>')
    return "\n".join(parts)


def _slide_scatter(resolved: dict, chart_b64: str) -> str:
    x_col, y_col, count_col = resolved["x_col"], resolved["y_col"], resolved["count_col"]
    return (
        f'<div class="slide">'
        f'<div class="label">Scatter, Sized By Volume</div>'
        f'<h2 class="title" style="font-size:1.7em">'
        f'{_esc(_humanize_col(y_col))} vs. {_esc(_humanize_col(x_col))}</h2>'
        f'<p style="color:#8aa8c8;margin-bottom:10px">'
        f'Same result, plotted differently — each point\'s size shows '
        f'{_esc(_humanize_col(count_col))}, so a category perched on an '
        f'extreme value backed by very few real records stands out from one '
        f'backed by a lot of them.</p>'
        f'<img class="chart" src="data:image/png;base64,{chart_b64}" alt="Scatter chart">'
        f'</div>'
    )


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
            slides.extend(_slides_issue_solution(tname, file_rec, llm=llm))

        # 6. Cleaned Data (before/after)
        for tname, rec in cleaning_map.items():
            if rec is None:
                continue
            entry_meta, file_rec = rec
            slides.append(_slide_cleaned_data(tname, file_rec))

    # 6.5. Question-specific data shaping — deterministic, separate from the
    # general cleaning slides above (present for both sql_analyst and
    # visualize: entries whenever the executed SQL has real shaping to show).
    try:
        shaping_slide = _slide_question_shaping(sql)
    except Exception:
        shaping_slide = None
    if shaping_slide:
        slides.append(shaping_slide)

    # 7. Making sense of the categories (visualize: entries only, when the
    # real result has a resolvable category column — never invented).
    if entry.get("chart_type"):
        try:
            resolved_cats = _resolve_category_values(entry)
        except Exception:
            resolved_cats = None
        if resolved_cats:
            cat_col, values = resolved_cats
            try:
                glossary = _generate_glossary(cat_col, values, llm)
            except Exception:
                glossary = {}
            if glossary:
                slides.append(_slide_glossary(cat_col, glossary))

    # 8. Visualization (visualize: entries only)
    if entry.get("chart_type") and entry.get("output_file_path"):
        slides.append(_slide_visualization(entry))

    # 9. Scatter, sized by volume (visualize: entries only, when the real
    # result has the right shape — category + count + 2 numeric measures).
    if entry.get("chart_type"):
        try:
            resolved_scatter = _resolve_scatter_columns(entry)
        except Exception:
            resolved_scatter = None
        if resolved_scatter:
            chart_b64 = _render_scatter_chart_b64(resolved_scatter)
            if chart_b64:
                slides.append(_slide_scatter(resolved_scatter, chart_b64))

    # 10. Summary
    slides.append(_slide_summary(summary_text))

    # ── Write to disk ──────────────────────────────────────────────────────────
    _PRESENTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{_slug(question)}_{ts_str}.html"
    out_path = _PRESENTATIONS_DIR / filename
    out_path.write_text(_assemble_html(slides, question[:60]), encoding="utf-8")
    return str(out_path)
