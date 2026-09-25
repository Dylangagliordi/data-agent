"""
Tests for Spec 8, Part 6: Notebook Export (utils/notebook_export.py).

Test 1-3 target build_notebook_from_steps directly with hand-built
NarrativeStep objects — pure, deterministic, no LLM/DB/file I/O.
Test 4 is a real integration test against a real logged entry (same
pattern tests/test_generate_report.py already uses), proving
render_notebook_export produces a real, valid .ipynb file end to end.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_notebook_export.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.narrative import NarrativeStep
from utils.notebook_export import build_notebook_from_steps, render_notebook_export

LOG_PATH = Path("logs/query_log.jsonl")


def test_build_notebook_from_steps_structure_and_part_headings():
    steps = [
        NarrativeStep(
            step_number=1, part="cleaning", title="Fixed placeholder values",
            explanation="Replaced -1 placeholders with real nulls.",
            technical_detail="df['rating'] = df['rating'].replace('-1', pd.NA)",
        ),
        NarrativeStep(
            step_number=2, part="cleaning", title="Removed duplicate rows",
            explanation="Dropped 3 fully duplicate rows.",
        ),
        NarrativeStep(
            step_number=3, part="analysis", title="Computed average rating",
            explanation="Averaged rating by industry.",
            technical_detail="SELECT industry, AVG(rating) FROM jobs GROUP BY industry",
            stats={"columns": ["industry", "avg_rating"], "rows": [["Tech", 4.1], ["Healthcare", 3.8]]},
        ),
    ]
    entry = {"user_question": "what is the average rating by industry", "curated_question": "What is the average rating by industry?"}

    notebook = build_notebook_from_steps(entry, steps)
    assert notebook["nbformat"] == 4
    assert notebook["cells"][0]["cell_type"] == "markdown"
    assert "average rating by industry" in "".join(notebook["cells"][0]["source"]).lower()

    all_text = json.dumps(notebook)
    assert "Part A: Data Cleaning" in all_text
    assert "Part C: Analysis" in all_text
    # Both cleaning steps share one part heading, not one per step.
    assert all_text.count("Part A: Data Cleaning") == 1

    code_cells = [c for c in notebook["cells"] if c["cell_type"] == "code"]
    assert len(code_cells) == 2, "only steps with a real technical_detail get a code cell"
    assert "pd.NA" in "".join(code_cells[0]["source"])

    # The full notebook must be valid, round-trippable JSON.
    json.loads(json.dumps(notebook))
    print("PASS: build_notebook_from_steps produces a valid nbformat-v4 structure with grouped part headings")


def test_build_notebook_from_steps_result_table_and_generic_stats():
    steps = [
        NarrativeStep(
            step_number=1, part="analysis", title="Ranked industries",
            explanation="Ranked by average rating.",
            stats={"columns": ["industry", "avg_rating"], "rows": [["Tech", 4.1]], "truncated": True},
        ),
        NarrativeStep(
            step_number=2, part="transformation", title="Split a composite column",
            explanation="Split city, state into two columns.",
            stats={"raw_column": "headquarters", "new_columns": "city, state"},
        ),
    ]
    notebook = build_notebook_from_steps({"user_question": "q"}, steps)
    all_text = "".join(
        "".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "markdown"
    )
    assert "| industry | avg_rating |" in all_text
    assert "Tech" in all_text and "4.1" in all_text
    assert "partial sample" in all_text
    assert "raw_column" in all_text and "headquarters" in all_text
    print("PASS: build_notebook_from_steps renders both the result-table stats shape and a generic stats dict")


def test_build_notebook_from_steps_empty():
    notebook = build_notebook_from_steps({"user_question": "q"}, [])
    all_text = json.dumps(notebook)
    assert "No narrative steps could be built" in all_text
    print("PASS: build_notebook_from_steps handles zero steps honestly, without fabricating content")


def test_render_notebook_export_real_entry():
    entries = [
        json.loads(l) for l in LOG_PATH.read_text().splitlines() if l.strip()
    ]
    sql_entries = [e for e in entries if e.get("route_response") in ("sql_analyst", "visualize")]
    assert sql_entries, "Need at least one sql_analyst/visualize entry in query_log.jsonl to run this test"
    entry = sql_entries[-1]

    path = render_notebook_export(entry)
    assert path.endswith(".ipynb")
    notebook = json.loads(Path(path).read_text())
    assert notebook["nbformat"] == 4
    assert len(notebook["cells"]) >= 1
    assert notebook["cells"][0]["cell_type"] == "markdown"
    print(f"PASS: render_notebook_export wrote a real, valid .ipynb file: {path}")


if __name__ == "__main__":
    test_build_notebook_from_steps_structure_and_part_headings()
    test_build_notebook_from_steps_result_table_and_generic_stats()
    test_build_notebook_from_steps_empty()
    test_render_notebook_export_real_entry()
    print("\nAll notebook_export tests passed.")
