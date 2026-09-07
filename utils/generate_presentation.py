"""Generate an HTML slideshow presentation from a query_log.jsonl entry —
Spec 2 (Final).

Public interface: generate_presentation(entry) -> str (path to HTML file).

Every slide (after the title slide) is rendered from the SAME shared,
ordered narrative walkthrough utils/generate_report.py renders as HTML
(utils.narrative.build_narrative_walkthrough) — one slide per NarrativeStep,
with a section-divider slide at the start of each `part` (Part A: Data
Cleaning, Part B: Transformation Options, Part C: Analysis), splitting a
step across multiple slides only when its explanation exceeds 120 words.
Every step from the shared list is rendered — no format-specific omission,
including the low-emphasis "other available transformations" note.
"""

import base64
import html
import re
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PRESENTATIONS_DIR = _PROJECT_ROOT / "presentations"

_PART_LABELS = {
    "cleaning": "Part A: Data Cleaning",
    "transformation": "Part B: Transformation Options",
    "analysis": "Part C: Analysis",
}
_MAX_WORDS_PER_SLIDE = 120


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _slug(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", question.lower())[:40].strip("_")


def _humanize_col(col: str) -> str:
    return str(col).replace("_", " ").title()


# ── Database lookups (own copy, title slide only) ──────────────────────────────

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


# ── Slide CSS and JS (unchanged look and feel) ──────────────────────────────────

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
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:.9em;
  background:#16213e;max-width:860px}
th,td{border:1px solid #253a5c;padding:8px 14px;text-align:left;vertical-align:top}
th{background:#1f3460;font-weight:600;color:#90c4ff}
td{color:#b8cce0}
.divider .title{font-size:3em}
.divider .subtitle{color:#5a7a9a;margin-top:14px;max-width:700px}
img.chart{max-height:60vh;max-width:100%;border:1px solid #253a5c;
  border-radius:6px;display:block;margin:14px 0}
.summary-text{font-size:1.12em;line-height:1.7;color:#c0d4e8;max-width:820px}
.note-slide .title{font-size:1.3em;color:#8aa8c8;font-weight:600}
.note-slide .summary-text{font-size:.98em;color:#9ab0c8}
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

def _slide_title(entry: dict, table_names: list, generated_at: str) -> str:
    question = entry.get("user_question") or entry.get("curated_question") or "Data Analysis"
    datasets = ", ".join(table_names) or "unknown dataset"
    return (
        f'<div class="slide">'
        f'<div class="label">Data Analysis Presentation</div>'
        f'<h1 class="title">{_esc(question[:120])}</h1>'
        f'<p class="subtitle">Dataset: {_esc(datasets)}</p>'
        f'<p class="subtitle" style="margin-top:8px;color:#3a5a7a">'
        f'Generated {_esc(generated_at)}</p>'
        f'</div>'
    )


def _slide_divider(part: str) -> str:
    label = _PART_LABELS.get(part, part.title())
    subtitle = {
        "cleaning": "What was checked, found, and fixed in the raw data.",
        "transformation": "Optional, judgment-call enrichment and style decisions.",
        "analysis": "How the data was shaped to answer this question, and the result.",
    }.get(part, "")
    return (
        f'<div class="slide divider">'
        f'<div class="label">Narrative Walkthrough</div>'
        f'<h1 class="title">{_esc(label)}</h1>'
        f'<p class="subtitle">{_esc(subtitle)}</p>'
        f'</div>'
    )


def _slide_checklist(entries: list) -> "str | None":
    """Spec 3, Part 4: one compact checklist slide per part, before the
    detailed narrative slides — None (slide omitted) for a part with zero
    steps, e.g. Part B on an entry with nothing surfaced."""
    if not entries:
        return None
    rows = []
    for e in entries:
        ref = f' — <span style="color:#6a8aaa">{_esc(e["reference_source"])}</span>' if e["reference_source"] else ""
        rows.append(f'<tr><td>{e["step_number"]}</td><td>{_esc(e["title"])}</td><td>{_esc(e["outcome"])}{ref}</td></tr>')
    return (
        '<div class="slide">'
        '<div class="label">Checklist</div>'
        '<h2 class="title" style="font-size:1.7em">At a glance</h2>'
        '<table><thead><tr><th>#</th><th>Step</th><th>Outcome</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        '</div>'
    )


def _split_words(text: str, max_words: int = _MAX_WORDS_PER_SLIDE) -> list:
    words = (text or "").split()
    if len(words) <= max_words:
        return [text or ""]
    return [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]


def _render_result_table_slide(stats: dict) -> str:
    cols = stats.get("columns") or []
    rows = stats.get("rows") or []
    if not cols or not rows:
        return ""
    human_cols = [_humanize_col(c) for c in cols]
    parts = ["<table><thead><tr>" + "".join(f"<th>{_esc(h)}</th>" for h in human_cols) + "</tr></thead><tbody>"]
    for row in rows[:10]:
        parts.append("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_glossary_table_slide(stats: dict) -> str:
    glossary = stats.get("glossary") or {}
    if not glossary:
        return ""
    rows = "".join(f"<tr><td>{_esc(t)}</td><td>{_esc(e)}</td></tr>" for t, e in glossary.items())
    return f"<table><thead><tr><th>Category</th><th>What it generally means</th></tr></thead><tbody>{rows}</tbody></table>"


def _render_scatter_image_slide(stats: dict) -> str:
    chart_b64 = stats.get("chart_b64", "")
    if not chart_b64:
        return ""
    return f'<img class="chart" src="data:image/png;base64,{chart_b64}" alt="Scatter chart">'


def _render_chart_image_slide(stats: dict) -> str:
    chart_image_path = stats.get("chart_image_path", "")
    if not chart_image_path:
        return '<p style="color:#6a8aaa">No chart image available for this entry.</p>'
    img_path = Path(chart_image_path)
    if not img_path.exists():
        return f'<p style="color:#6a8aaa">Chart image not found at: {_esc(chart_image_path)}</p>'
    b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return f'<img class="chart" src="data:image/png;base64,{b64}" alt="Chart">'


def _slides_for_step(step) -> list:
    """One slide per NarrativeStep, splitting only when its explanation
    exceeds 120 words (Spec 2, section 3). The low-emphasis Part B note
    (stats["low_emphasis"]) gets a smaller, muted-styled slide instead of a
    numbered "Step N:" title — but is still rendered, never omitted. Extra
    real content (the final result table, the chart image) is attached only
    to the LAST chunk of a split step, so it isn't duplicated across slides.
    """
    label = _esc(_PART_LABELS.get(step.part, step.part.title()))
    chunks = _split_words(step.explanation)
    slides = []

    if step.stats.get("low_emphasis"):
        return [
            f'<div class="slide note-slide">'
            f'<div class="label">{label}</div>'
            f'<h2 class="title">{_esc(step.title)}</h2>'
            f'<p class="summary-text">{_esc(step.explanation)}</p>'
            f'</div>'
        ]

    for i, chunk in enumerate(chunks):
        title = _esc(step.title)
        if len(chunks) > 1:
            title += f" ({i + 1}/{len(chunks)})"
        parts = [
            '<div class="slide">',
            f'<div class="label">{label} — Step {step.step_number}</div>',
            f'<h2 class="title" style="font-size:1.7em">{title}</h2>',
            f'<p class="summary-text">{_esc(chunk)}</p>',
        ]
        is_last_chunk = i == len(chunks) - 1
        if is_last_chunk and step.part == "analysis" and step.title == "The final result":
            table_html = _render_result_table_slide(step.stats)
            if table_html:
                parts.append(table_html)
        if is_last_chunk and step.part == "analysis" and step.title.startswith("Visualize the result"):
            parts.append(_render_chart_image_slide(step.stats))
        if is_last_chunk and step.part == "analysis" and step.title == "What do these categories mean?":
            parts.append(_render_glossary_table_slide(step.stats))
        if is_last_chunk and step.part == "analysis" and step.title == "Scatter, sized by volume":
            parts.append(_render_scatter_image_slide(step.stats))
        parts.append('</div>')
        slides.append("\n".join(parts))

    return slides


# ── Full HTML assembly ─────────────────────────────────────────────────────────

def _assemble_html(slides: list, title: str) -> str:
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
    from utils.manual_mode import build_step_checklist
    from utils.narrative import assemble_full_walkthrough, build_narrative_walkthrough

    question = entry.get("user_question") or entry.get("curated_question") or "presentation"
    sql = entry.get("generated_sql_query", "")
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    try:
        known_tables = _all_table_names()
        touched_tables = _detect_tables_in_sql(sql, known_tables) if sql else []
    except Exception:
        touched_tables = []

    try:
        steps = assemble_full_walkthrough(entry, pick_llm("cheap"))
    except Exception:
        steps = build_narrative_walkthrough(entry)  # deterministic fallback, never fully fails

    checklist = build_step_checklist(steps)

    slides: list = [_slide_title(entry, touched_tables, generated_at)]

    current_part = None
    for step in steps:
        if step.part != current_part:
            current_part = step.part
            slides.append(_slide_divider(current_part))
            # Spec 3, Part 4: one checklist-style slide per part, before the
            # detailed narrative slides — derived from the exact same
            # checklist entries, never a second source of truth.
            part_entries = [c for c in checklist if c["part"] == current_part]
            checklist_slide = _slide_checklist(part_entries)
            if checklist_slide:
                slides.append(checklist_slide)
        slides.extend(_slides_for_step(step))

    _PRESENTATIONS_DIR.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{_slug(question)}_{ts_str}.html"
    out_path = _PRESENTATIONS_DIR / filename
    out_path.write_text(_assemble_html(slides, question[:60]), encoding="utf-8")
    return str(out_path)
