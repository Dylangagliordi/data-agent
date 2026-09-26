"""Generate an HTML report from a query_log.jsonl entry — Spec 2 (Final).

Public interface: generate_report(entry) -> str (path to the written HTML file).

The ENTIRE report body is rendered from ONE shared, ordered narrative
walkthrough (utils.narrative.build_narrative_walkthrough), grouped into three
sections matching each step's `part` — Part A: Data Cleaning, Part B:
Transformation Options, Part C: Analysis — with one `<h2>Step N: <title>
</h2>` block per step, in order. utils/generate_presentation.py renders the
exact same walkthrough as slides instead, so the two documents always agree
in sequence and content, differing only in HTML-vs-slide formatting.

Every fact in the walkthrough itself traces back to real, logged data
(cleaning_log.jsonl, the live DB schema, the real Transformation Options
decision log, and a deterministic parse of the executed SQL) — see
utils/narrative.py's own module docstring for the anti-fabrication
discipline governing every step's content.
"""

import base64
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_QUERY_LOG_PATH = _PROJECT_ROOT / "logs" / "query_log.jsonl"
_REPORTS_DIR = _PROJECT_ROOT / "reports"

_PART_TITLES = {
    "cleaning": "Part A: Data Cleaning",
    "transformation": "Part B: Transformation Options",
    "analysis": "Part C: Analysis",
}


# ── HTML helpers ───────────────────────────────────────────────────────────────

def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _slug(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", question.lower())[:40].strip("_")


_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:940px;margin:48px auto;padding:0 24px;color:#222;line-height:1.6}
h1{color:#111;border-bottom:2px solid #ddd;padding-bottom:12px;font-size:1.6em;margin-top:48px}
h1:first-of-type{margin-top:0}
h2{color:#1a3a5c;margin-top:32px;font-size:1.15em;border-left:4px solid #4a90d9;padding-left:10px}
table{border-collapse:collapse;width:100%;margin:14px 0;font-size:0.93em}
th,td{border:1px solid #ddd;padding:7px 12px;text-align:left;vertical-align:top}
th{background:#f2f5f9;font-weight:600}
.meta{color:#666;font-size:0.88em;margin-top:-8px}
pre{font-family:'SFMono-Regular',Consolas,monospace;background:#f6f8fa;padding:14px;border-radius:6px;white-space:pre-wrap;font-size:0.88em;border:1px solid #e0e0e0}
.note{background:#fff8e1;border-left:4px solid #f9a825;padding:10px 14px;border-radius:3px;margin:12px 0;font-size:0.92em}
.ok{color:#2e7d32;font-weight:600}
.bad{color:#c62828;font-weight:600}
ul{margin:8px 0;padding-left:22px}
li{margin:3px 0}
table.checklist{font-size:0.85em;background:#fafbfc;margin:8px 0 24px}
table.checklist th{background:#eef2f7;font-weight:600;color:#444}
table.checklist td{color:#555}
"""


def _html_page(title: str, body: str, generated_at: str) -> str:
    return (
        f"<!DOCTYPE html>\n<html lang='en'>\n<head>\n"
        f"<meta charset='UTF-8'>\n"
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        f"</head>\n<body>\n"
        f"<h1 style='border:none;padding-bottom:0;margin-top:0'>{_esc(title)}</h1>\n"
        f"<p class='meta'>Generated {_esc(generated_at)}</p>\n"
        f"{body}\n"
        f"</body>\n</html>\n"
    )


# ── Narrative-step rendering ────────────────────────────────────────────────

def _render_result_table(stats: dict) -> str:
    cols = stats.get("columns") or []
    rows = stats.get("rows") or []
    if not cols or not rows:
        return ""
    human_cols = [str(c).replace("_", " ").title() for c in cols]
    parts = []
    if stats.get("truncated"):
        parts.append(
            "<div class='note'>This result was capped before reaching this table — "
            "the rows below are a partial sample, not the complete result set.</div>"
        )
    parts.append("<table><thead><tr>" + "".join(f"<th>{_esc(h)}</th>" for h in human_cols) + "</tr></thead><tbody>")
    for row in rows:
        parts.append("<tr>" + "".join(f"<td>{_esc(v)}</td>" for v in row) + "</tr>")
    parts.append("</tbody></table>")
    return "\n".join(parts)


def _render_chart_image(stats: dict) -> str:
    chart_image_path = stats.get("chart_image_path", "")
    if not chart_image_path:
        return (
            "<div class='note'>No chart image is available for this entry — "
            "either it predates automatic image rendering or rendering failed.</div>"
        )
    img_path = Path(chart_image_path)
    if not img_path.exists():
        return (
            "<div class='note'>Chart image file was referenced in the log but "
            "could not be found on disk — it may have been moved or deleted.</div>"
        )
    b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
    return (
        f'<img src="data:image/png;base64,{b64}" '
        f'style="max-width:100%;height:auto;border:1px solid #ddd;'
        f'border-radius:6px;margin:12px 0;display:block" alt="Chart">'
    )


def _checklist_html(entries: list) -> str:
    """Spec 3, Part 4: a compact, scannable checklist for one part, rendered
    from build_step_checklist's output (itself derived purely from the same
    NarrativeStep.stats every step below is rendered from — never a second
    source of truth). Empty for a part with zero steps."""
    if not entries:
        return ""
    rows = []
    for e in entries:
        ref = f" — <em>{_esc(e['reference_source'])}</em>" if e["reference_source"] else ""
        rows.append(
            f"<tr><td>{e['step_number']}</td><td>{_esc(e['title'])}</td>"
            f"<td>{_esc(e['outcome'])}{ref}</td></tr>"
        )
    return (
        "<table class='checklist'><thead><tr><th>#</th><th>Step</th><th>Outcome</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _step_html(step) -> str:
    """Render one NarrativeStep. A step whose stats mark it low_emphasis
    (Part B's "other available transformations" note, per Spec 2 — real, but
    explicitly "not a full step") renders as a plain note, not a numbered
    `<h2>Step N:</h2>` heading; every other step gets the full treatment.
    The final-result step additionally embeds the real result table, and the
    chart step embeds the real chart image — reusing the already-computed
    data on the step's own stats rather than rebuilding either.
    """
    if step.stats.get("low_emphasis"):
        return f"<div class='note'><strong>{_esc(step.title)}:</strong> {_esc(step.explanation)}</div>"

    parts = [f"<h2>Step {step.step_number}: {_esc(step.title)}</h2>", f"<p>{_esc(step.explanation)}</p>"]

    if step.technical_detail:
        parts.append(
            "<details><summary style='cursor:pointer;color:#4a90d9;font-size:0.88em'>"
            "Real, verbatim detail behind this step</summary>"
            f"<pre>{_esc(step.technical_detail)}</pre></details>"
        )

    if step.part == "analysis" and step.title == "The final result":
        table_html = _render_result_table(step.stats)
        if table_html:
            parts.append(table_html)

    if step.part == "analysis" and step.title.startswith("Visualize the result"):
        parts.append(_render_chart_image(step.stats))

    if step.part == "analysis" and step.title == "What do these categories mean?":
        glossary = step.stats.get("glossary") or {}
        if glossary:
            rows = "".join(f"<tr><td>{_esc(t)}</td><td>{_esc(e)}</td></tr>" for t, e in glossary.items())
            parts.append(f"<table><thead><tr><th>Category</th><th>What it generally means</th></tr></thead><tbody>{rows}</tbody></table>")

    if step.part == "analysis" and step.title == "Scatter, sized by volume":
        chart_b64 = step.stats.get("chart_b64", "")
        if chart_b64:
            parts.append(
                f'<img src="data:image/png;base64,{chart_b64}" '
                f'style="max-width:100%;height:auto;border:1px solid #ddd;'
                f'border-radius:6px;margin:12px 0;display:block" alt="Scatter chart">'
            )

    return "\n".join(parts)


# ── Public entry point ─────────────────────────────────────────────────────────

def generate_report(entry: dict, precomputed_steps: "list | None" = None) -> str:
    """Build an HTML report from a query_log.jsonl entry dict.
    Returns the path to the written HTML file.

    precomputed_steps (Spec 16, Part 2): when given, these NarrativeStep
    objects are rendered directly instead of deriving them from `entry` via
    assemble_full_walkthrough/build_narrative_walkthrough — used by the
    Conductor (agents/conductor.py), whose real multi-tool-call trace doesn't
    fit the single-question query_log.jsonl entry shape those two functions
    expect. None (the default) reproduces this function's exact prior
    behavior for every existing caller.
    """
    question = entry.get("user_question") or entry.get("curated_question") or "report"
    # Prefer the cleaned-up question for the human-facing title — user_question
    # still carries any router prefix (e.g. "Visualize: ") verbatim, which reads
    # poorly when hard-truncated to 60 chars; curated_question is the same
    # question with only wording cleanup applied.
    title_question = entry.get("curated_question") or question
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if precomputed_steps is not None:
        steps = precomputed_steps
    else:
        from utils.llm_pick import pick_llm
        from utils.narrative import assemble_full_walkthrough, build_narrative_walkthrough

        try:
            steps = assemble_full_walkthrough(entry, pick_llm("cheap"))
        except Exception:
            steps = build_narrative_walkthrough(entry)  # deterministic fallback, never fully fails

    from utils.manual_mode import build_step_checklist

    checklist = build_step_checklist(steps)

    body_parts = []
    current_part = None
    for step in steps:
        if step.part != current_part:
            current_part = step.part
            body_parts.append(f"<h1>{_esc(_PART_TITLES.get(current_part, current_part.title()))}</h1>")
            # Part 4: a compact checklist at the top of each part, immediately
            # followed by the full prose below — derived from the SAME
            # checklist entries every step below also came from, never a
            # second source of truth.
            part_entries = [c for c in checklist if c["part"] == current_part]
            body_parts.append(_checklist_html(part_entries))
        body_parts.append(_step_html(step))
    body = "\n\n".join(body_parts) if body_parts else "<p>No narrative steps could be built for this entry.</p>"

    title = f"Report: {title_question[:60]}"
    full_html = _html_page(title, body, generated_at)

    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{_slug(question)}_{ts_str}.html"
    out_path = _REPORTS_DIR / filename
    out_path.write_text(full_html, encoding="utf-8")
    return str(out_path)


def last_query_log_entry() -> "dict | None":
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
