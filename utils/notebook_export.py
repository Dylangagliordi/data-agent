"""Notebook Export (Spec 8, Part 6): render a run's real narrative walkthrough
as a Colab/Jupyter-style .ipynb file — the same underlying facts
utils/generate_report.py renders as an HTML report and
utils/generate_presentation.py renders as slides, presented instead as the
cell-by-cell record format this project's other, real data-science notebook
(the salary_satisfaction_analysis.ipynb companion repo) uses.

build_notebook_from_steps(entry, steps) is a PURE, deterministic assembly
(no LLM, no DB) — exactly like utils.narrative.build_narrative_walkthrough
itself — so it's trivially testable with hand-built NarrativeStep objects.
render_notebook_export(entry) is the thin wrapper that gets the real
(optionally LLM-narrated) steps and writes the file, mirroring
utils.generate_report.generate_report's own try/except fallback to the
deterministic walkthrough if narration fails.

No code cell here is ever executed — this presents the historical record of
what a run actually did (the real SQL / generated fix code, verbatim), it
never re-runs anything.

CLI:
    python main.py "notebook: <question>"   (runs fresh, then exports)
    python main.py "notebook last"          (exports the most recent run)
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "notebook_exports"


def _slug(question: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", question.lower())[:40].strip("_") or "run"


def _markdown_cell(text: str) -> dict:
    return {"cell_type": "markdown", "metadata": {}, "source": text.splitlines(keepends=True)}


def _code_cell(code: str) -> dict:
    return {
        "cell_type": "code",
        "execution_count": None,
        "metadata": {},
        "outputs": [],
        "source": code.splitlines(keepends=True),
    }


def _stats_to_markdown(stats: dict) -> str:
    """Renders a step's real `stats` dict as a small markdown block. The
    {"columns": [...], "rows": [...]} result-table shape (the same one
    utils/generate_report.py:_render_result_table renders as an HTML table)
    becomes a markdown table; any other stats shape becomes a plain bullet
    list of its real key/value pairs — never invented, never dropped."""
    if not stats:
        return ""
    cols = stats.get("columns")
    rows = stats.get("rows")
    if cols and rows:
        header = "| " + " | ".join(str(c) for c in cols) + " |"
        divider = "| " + " | ".join("---" for _ in cols) + " |"
        body_lines = [
            "| " + " | ".join(str(v) for v in row) + " |" for row in rows
        ]
        lines = ["**Result:**", "", header, divider, *body_lines]
        if stats.get("truncated"):
            lines.append("")
            lines.append("_This result was capped — the rows above are a partial sample._")
        return "\n".join(lines) + "\n"

    other_keys = {k: v for k, v in stats.items() if k not in ("columns", "rows", "truncated")}
    if not other_keys:
        return ""
    bullet_lines = [f"- **{k}**: {v}" for k, v in other_keys.items()]
    return "\n".join(["**Details:**", "", *bullet_lines]) + "\n"


def _step_to_cells(step) -> list:
    header = f"## Step {step.step_number}: {step.title}"
    body = step.explanation or ""
    cells = [_markdown_cell(f"{header}\n\n{body}\n")]
    if step.technical_detail:
        cells.append(_code_cell(step.technical_detail))
    stats_md = _stats_to_markdown(step.stats)
    if stats_md:
        cells.append(_markdown_cell(stats_md))
    return cells


_PART_HEADINGS = {
    "cleaning": "Part A: Data Cleaning",
    "transformation": "Part B: Transformation Options",
    "analysis": "Part C: Analysis",
}


def build_notebook_from_steps(entry: dict, steps: list) -> dict:
    """Pure assembly of a valid nbformat-v4 notebook dict from a real entry
    and its already-built narrative steps. No LLM call, no file I/O — this is
    the deterministic half render_notebook_export wraps."""
    question = entry.get("user_question") or entry.get("curated_question") or "Data agent run"
    title_question = entry.get("curated_question") or question
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    cells = [
        _markdown_cell(
            f"# {title_question}\n\n"
            f"_Exported {generated_at} from the data agent's real, logged run — "
            f"every cell below reflects what actually happened, nothing re-executed._\n"
        )
    ]

    seen_parts = set()
    for step in steps:
        if step.part not in seen_parts:
            heading = _PART_HEADINGS.get(step.part, step.part.title())
            cells.append(_markdown_cell(f"# {heading}\n"))
            seen_parts.add(step.part)
        cells.extend(_step_to_cells(step))

    if not steps:
        cells.append(_markdown_cell("_No narrative steps could be built for this entry._\n"))

    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def render_notebook_export(entry: dict) -> str:
    """Builds the real (optionally LLM-narrated) walkthrough for entry and
    writes it as a .ipynb file under notebook_exports/. Returns the written
    path. Falls back to the deterministic walkthrough if narration fails,
    exactly like utils.generate_report.generate_report does."""
    from utils.llm_pick import pick_llm
    from utils.narrative import assemble_full_walkthrough, build_narrative_walkthrough

    try:
        steps = assemble_full_walkthrough(entry, pick_llm("cheap"))
    except Exception:
        steps = build_narrative_walkthrough(entry)

    notebook = build_notebook_from_steps(entry, steps)

    question = entry.get("user_question") or entry.get("curated_question") or "run"
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"{_slug(question)}_{ts_str}.ipynb"
    out_path.write_text(json.dumps(notebook, indent=1), encoding="utf-8")
    return str(out_path)
