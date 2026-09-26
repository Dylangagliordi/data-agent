"""
Tests for Spec 16, Part 2: Conductor narrative assembly + reporting
integration (utils/narrative.py:build_conductor_narrative, and the
precomputed_steps parameter added to generate_report/generate_presentation/
render_notebook_export).

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_conductor_narrative.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.generate_presentation import generate_presentation
from utils.generate_report import generate_report
from utils.narrative import build_conductor_narrative
from utils.notebook_export import render_notebook_export


def test_build_conductor_narrative_real_trace():
    tool_calls = [
        {"tool": "profile_table", "input": {"table_name": "orders"}, "output": "Auto-EDA profile written to /fake/profile.html"},
        {"tool": "ask_question", "input": {"question": "average order value"}, "output": "The average order value is $137."},
    ]
    steps = build_conductor_narrative("understand orders and get the average value", tool_calls)

    assert steps[0].part == "conductor"
    assert steps[0].title == "Goal"
    assert "understand orders" in steps[0].explanation

    assert len(steps) == 3  # 1 goal step + 2 real tool-call steps
    assert steps[1].title == "Called profile_table"
    assert "profile_table" in steps[1].explanation
    assert "{'table_name': 'orders'}" in steps[1].explanation
    assert "/fake/profile.html" in steps[1].explanation

    assert steps[2].title == "Called ask_question"
    assert "$137" in steps[2].explanation
    assert "$137" in steps[2].technical_detail

    step_numbers = [s.step_number for s in steps]
    assert step_numbers == [1, 2, 3], "steps must be numbered sequentially, matching the real call order"
    print("PASS: build_conductor_narrative turns a real tool-call trace into real, non-fabricated steps")


def test_build_conductor_narrative_empty_trace_is_honest():
    steps = build_conductor_narrative("a goal answered with no investigation", [])
    assert len(steps) == 2
    assert steps[1].title == "No investigation needed"
    assert "no tool calls" in steps[1].explanation.lower()
    print("PASS: an empty trace produces an honest step, never a fabricated investigation")


def test_generate_report_with_precomputed_conductor_steps():
    tool_calls = [
        {"tool": "check_joins", "input": {}, "output": "Join advisory written to /fake/joins.html"},
    ]
    steps = build_conductor_narrative("map out how these tables relate", tool_calls)
    entry = {"user_question": "map out how these tables relate", "curated_question": "map out how these tables relate"}

    path = generate_report(entry, precomputed_steps=steps)
    content = open(path).read()
    assert "Conductor" in content, "an unrecognized part value must still render a real section header"
    assert "check_joins" in content
    assert "joins.html" in content
    print(f"PASS: generate_report renders a real conductor trace via precomputed_steps: {path}")


def test_generate_presentation_with_precomputed_conductor_steps():
    tool_calls = [
        {"tool": "check_dq_backlog", "input": {}, "output": "DQ backlog written to /fake/dq.html"},
    ]
    steps = build_conductor_narrative("check data quality across the board", tool_calls)
    entry = {"user_question": "check data quality across the board", "curated_question": "check data quality across the board"}

    path = generate_presentation(entry, precomputed_steps=steps)
    content = open(path).read()
    assert "check_dq_backlog" in content
    print(f"PASS: generate_presentation renders a real conductor trace via precomputed_steps: {path}")


def test_notebook_export_with_precomputed_conductor_steps():
    tool_calls = [
        {"tool": "profile_table", "input": {"table_name": "sellers"}, "output": "profile written to /fake/p.html"},
    ]
    steps = build_conductor_narrative("profile the sellers table", tool_calls)
    entry = {"user_question": "profile the sellers table", "curated_question": "profile the sellers table"}

    path = render_notebook_export(entry, precomputed_steps=steps)
    import json

    notebook = json.loads(open(path).read())
    all_text = json.dumps(notebook)
    assert "profile_table" in all_text
    assert "Conductor" in all_text
    print(f"PASS: render_notebook_export renders a real conductor trace via precomputed_steps: {path}")


def test_full_conductor_to_report_pipeline_real_live():
    """End-to-end proof, no fakes: a real conductor run, its real trace, and a
    real report built from it — closing the exact gap identified earlier in
    conversation (the reporting pipeline didn't know how to represent a
    multi-step conductor journey)."""
    from agents.conductor import run_conductor

    result = run_conductor(
        "Give me a quick sense of the olist_sellers_dataset table, then tell me how many rows it has.",
        max_steps=6,
    )
    steps = build_conductor_narrative(
        "Give me a quick sense of the olist_sellers_dataset table, then tell me how many rows it has.",
        result["tool_calls"],
    )
    entry = {"user_question": "conductor run", "curated_question": "conductor run"}
    path = generate_report(entry, precomputed_steps=steps)
    content = open(path).read()
    assert "Conductor" in content
    for call in result["tool_calls"]:
        assert call["tool"] in content, f"expected the real tool name {call['tool']!r} to appear in the report"
    print(f"PASS: a real, live conductor run renders end to end into a real, coherent report: {path}")


if __name__ == "__main__":
    test_build_conductor_narrative_real_trace()
    test_build_conductor_narrative_empty_trace_is_honest()
    test_generate_report_with_precomputed_conductor_steps()
    test_generate_presentation_with_precomputed_conductor_steps()
    test_notebook_export_with_precomputed_conductor_steps()
    test_full_conductor_to_report_pipeline_real_live()
    print("\nAll conductor_narrative tests passed.")
