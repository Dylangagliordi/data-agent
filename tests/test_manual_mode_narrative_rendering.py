"""Spec 3, Part 4: end-to-end rendering of a manual-mode transformation
decision through the shared narrative walkthrough into both
generate_report.py and generate_presentation.py — the checklist appears at
the top of Part B in both, shows "Pinned to reference" with the real
citation, and the manual-mode step's own prose never claims a fabricated
reason (mirrors the anti-fabrication discipline already tested directly on
utils/narrative.py:_add_transformation_step in test_narrative_walkthrough.py,
here confirmed through the full document-rendering pipeline instead).
"""

from pathlib import Path

from utils.generate_presentation import generate_presentation
from utils.generate_report import generate_report
from utils.manual_mode import build_step_checklist
from utils.narrative import build_narrative_walkthrough

MANUAL_ENTRY = {
    "generated_sql_query": "SELECT industry_category, COUNT(*) FROM uncleaned_ds_jobs GROUP BY industry_category",
    "sql_query_execution_result": '{"columns": ["industry_category", "n"], "rows": [["Technology", 5]], "truncated": false}',
    "final_answer": "Technology has 5 records.",
    "chart_type": "",
    "transformation_narrative_log": [
        {
            "table_name": "uncleaned_ds_jobs",
            "candidate": {
                "candidate_id": "x1", "kind": "categorical_consolidation", "columns": ["Industry"],
                "description": "d", "relevance_tags": ["industry"],
            },
            "chosen_option_id": "apply",
            "reasoning_shown": {
                "source": "manual_mode",
                "reference": "Reference deck: 'Job Market', slide 6",
                "supplied_data": {"Tech": "Technology", "Finance": "Financial Services"},
            },
            "fresh": True,
            "reload_reask": False,
            "decided_at": None,
        },
    ],
    "transformation_candidates_not_relevant": [],
}

print("=" * 70)
print("TEST 1: the narrative step never claims a fabricated reason and")
print("marks itself manual_mode with the real reference_source")
print("=" * 70)

steps = build_narrative_walkthrough(MANUAL_ENTRY)
mm_step = next(s for s in steps if s.stats.get("manual_mode"))
assert mm_step.stats["reference_source"] == "Reference deck: 'Job Market', slide 6"
assert "pinned directly to a supplied reference" in mm_step.explanation
assert "no additional reason" not in mm_step.explanation.lower(), (
    "a manual-mode step must never use the 'no reason was recorded' framing — "
    "the reference IS the reason"
)
print(f"PASS: deterministic step explanation: {mm_step.explanation!r}\n")

checklist = build_step_checklist(steps)
mm_entry = next(c for c in checklist if c["step_number"] == mm_step.step_number)
assert mm_entry["outcome"] == "Pinned to reference"
assert mm_entry["reference_source"] == "Reference deck: 'Job Market', slide 6"
print(f"PASS: checklist entry: {mm_entry}\n")

print("=" * 70)
print("TEST 2: generate_report() renders the checklist at the top of Part B,")
print("followed by the full prose, with the real reference visible in both")
print("=" * 70)

report_path = generate_report(MANUAL_ENTRY)
report_html = Path(report_path).read_text()

part_b_idx = report_html.find("Part B: Transformation Options")
checklist_idx = report_html.find("class='checklist'", part_b_idx)
step_heading_idx = report_html.find(f"Step {mm_step.step_number}:", part_b_idx)
assert part_b_idx != -1 and checklist_idx != -1 and step_heading_idx != -1
assert part_b_idx < checklist_idx < step_heading_idx, (
    "checklist must appear at the top of Part B, before the step's own full prose heading"
)
assert "Pinned to reference" in report_html[checklist_idx:step_heading_idx]
assert "Job Market" in report_html[checklist_idx:step_heading_idx]
print(f"PASS: report {report_path} — checklist precedes full prose in Part B, both show the reference.\n")

print("=" * 70)
print("TEST 3: generate_presentation() renders a checklist slide before the")
print("detailed narrative slides for Part B, same reference visible")
print("=" * 70)

pres_path = generate_presentation(MANUAL_ENTRY)
pres_html = Path(pres_path).read_text()

part_b_divider_idx = pres_html.find("Part B: Transformation Options")
checklist_slide_idx = pres_html.find('"label">Checklist', part_b_divider_idx)
step_slide_idx = pres_html.find(f"Part B: Transformation Options — Step {mm_step.step_number}", part_b_divider_idx)
assert part_b_divider_idx != -1 and checklist_slide_idx != -1 and step_slide_idx != -1
assert part_b_divider_idx < checklist_slide_idx < step_slide_idx, (
    "the checklist slide must come before the detailed step slide"
)
assert "Job Market" in pres_html[checklist_slide_idx:step_slide_idx]
print(f"PASS: presentation {pres_path} — checklist slide precedes the detailed step slide, "
      "reference visible.\n")

print("=" * 70)
print("TEST 4: an entry with no Part B content produces no Part B checklist")
print("in either document (checklist never fabricates a part that has")
print("nothing to show)")
print("=" * 70)

no_b_entry = dict(MANUAL_ENTRY, transformation_narrative_log=[])
steps_no_b = build_narrative_walkthrough(no_b_entry)
assert not any(s.part == "transformation" for s in steps_no_b)
checklist_no_b = build_step_checklist(steps_no_b)
assert not any(c["part"] == "transformation" for c in checklist_no_b)

report_no_b = Path(generate_report(no_b_entry)).read_text()
assert "Part B: Transformation Options" not in report_no_b
pres_no_b = Path(generate_presentation(no_b_entry)).read_text()
assert "Part B: Transformation Options" not in pres_no_b
print("PASS: Part B (heading, checklist, and slides) is cleanly omitted end-to-end when empty.\n")

print("=" * 70)
print("ALL MANUAL-MODE NARRATIVE-RENDERING (SPEC 3, PART 4) ASSERTIONS PASSED")
print("=" * 70)
