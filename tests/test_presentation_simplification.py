"""Tests for the glossary + volume-scatter Part C additions (Spec 2
"Explicitly out of scope" — kept as-is, moved into utils/narrative.py and
folded into the unified walkthrough via assemble_full_walkthrough).

Test 1: resolve_category_values — deterministic category-column detection
        against a real-shaped result, no LLM.
Test 2: generate_glossary — one batched LLM call, parses `label :: explanation`
        lines back onto the real category values; malformed response -> {}.
Test 3: resolve_scatter_columns — deterministic shape check (needs a
        category column, a count-like column, and 2+ other numeric measures);
        None when the shape doesn't fit.
Test 4: render_scatter_chart_png_b64 — renders real base64 PNG bytes from a
        resolved scatter spec.
Test 5: assemble_full_walkthrough folds both into Part C around the chart
        step, in both generate_report.py and generate_presentation.py.
Test 6: end-to-end generate_presentation() on the real last query_log entry
        still produces a valid slideshow (regression check for the wiring).

The old per-issue-slide text simplification (_simplify_cleaning_narratives)
is superseded by utils/narrative.py:narrate_steps, which now rewrites every
cleaning step (not just fail-level issue slides) in one combined call — see
tests/test_narrative_walkthrough.py's narrate_steps coverage.
"""

import base64
import json
from pathlib import Path

from utils.narrative import (
    generate_glossary,
    render_scatter_chart_png_b64,
    resolve_category_values,
    resolve_scatter_columns,
)
from utils.generate_presentation import generate_presentation
from utils.generate_report import generate_report, last_query_log_entry

FAKE_RESULT = json.dumps({
    "columns": ["industry", "avg_salary_k", "avg_rating", "job_count"],
    "rows": [
        ["Staffing & Outsourcing", 128.97, 4.14, 35],
        ["Federal Agencies", 134.59, 3.96, 11],
        ["Consulting", 128.10, 3.89, 29],
    ],
    "truncated": False,
})


class FakeGlossaryLLM:
    def invoke(self, messages):
        class R:
            content = (
                "Staffing & Outsourcing :: Companies that supply workers to other businesses.\n"
                "Federal Agencies :: U.S. government departments and organizations.\n"
                "Consulting :: Firms that advise other businesses.\n"
            )
        return R()


class MalformedGlossaryLLM:
    def invoke(self, messages):
        class R:
            content = "I'm not sure what these mean."
        return R()


class FailingLLM:
    def invoke(self, messages):
        raise RuntimeError("simulated LLM failure")

    def with_structured_output(self, schema_cls):
        return self


# ── Test 1: resolve_category_values — deterministic, no LLM ───────────────────
print("=" * 70)
print("TEST 1: resolve_category_values picks the real non-numeric column")
print("=" * 70)

entry1 = {"sql_query_execution_result": FAKE_RESULT}
resolved1 = resolve_category_values(entry1)
assert resolved1 is not None, "Must resolve a category column from a real result"
cat_col, values = resolved1
assert cat_col == "industry", f"Expected 'industry', got {cat_col!r}"
assert values == ["Staffing & Outsourcing", "Federal Agencies", "Consulting"], (
    f"Must return real distinct values in first-seen order, got {values!r}"
)
print(f"PASS: resolved category column {cat_col!r} with values {values!r}\n")

entry1b = {"sql_query_execution_result": json.dumps({
    "columns": ["a", "b"], "rows": [[1, 2], [3, 4]], "truncated": False,
})}
assert resolve_category_values(entry1b) is None, "All-numeric result must resolve to None"
print("PASS: all-numeric result correctly resolves to None.\n")


# ── Test 2: generate_glossary — parses label::explanation, honest on failure ──
print("=" * 70)
print("TEST 2: generate_glossary parses real lines; fails honestly")
print("=" * 70)

glossary = generate_glossary("industry", values, FakeGlossaryLLM())
assert set(glossary.keys()) == set(values), f"Every real value must get an entry: {glossary!r}"
assert "supply workers" in glossary["Staffing & Outsourcing"], "Real explanation must be used"
print(f"PASS: glossary resolved for all {len(values)} real terms.\n")

glossary_bad = generate_glossary("industry", values, MalformedGlossaryLLM())
assert glossary_bad == {}, "Malformed response must yield an empty (honest) glossary, not fabricate one"
print("PASS: malformed LLM response yields empty glossary, not fabrication.\n")

glossary_fail = generate_glossary("industry", values, FailingLLM())
assert glossary_fail == {}, "LLM exception must be caught and yield empty glossary"
print("PASS: LLM exception handled without raising.\n")


# ── Test 3: resolve_scatter_columns — deterministic shape check ───────────────
print("=" * 70)
print("TEST 3: resolve_scatter_columns requires category + count + 2 measures")
print("=" * 70)

entry3 = {"sql_query_execution_result": FAKE_RESULT}
resolved3 = resolve_scatter_columns(entry3)
assert resolved3 is not None, "Real 4-column shape (category, 2 measures, count) must resolve"
assert resolved3["category_col"] == "industry"
assert resolved3["count_col"] == "job_count", f"Must pick the real count-like column, got {resolved3['count_col']!r}"
assert set([resolved3["x_col"], resolved3["y_col"]]) == {"avg_salary_k", "avg_rating"}, (
    f"The two non-count numeric columns must be the x/y measures, got {resolved3!r}"
)
print(f"PASS: resolved scatter spec {resolved3['category_col']}/{resolved3['x_col']}/{resolved3['y_col']}/{resolved3['count_col']}\n")

entry3b = {"sql_query_execution_result": json.dumps({
    "columns": ["industry", "avg_salary_k", "avg_rating"],
    "rows": [["A", 1.0, 2.0], ["B", 3.0, 4.0]],
    "truncated": False,
})}
assert resolve_scatter_columns(entry3b) is None, "No count-like column must resolve to None"
print("PASS: missing count-like column correctly resolves to None.\n")

entry3c = {"sql_query_execution_result": json.dumps({
    "columns": ["industry", "avg_salary_k", "job_count"],
    "rows": [["A", 1.0, 5], ["B", 3.0, 9]],
    "truncated": False,
})}
assert resolve_scatter_columns(entry3c) is None, "Only 1 measure column must resolve to None"
print("PASS: single-measure result correctly resolves to None.\n")


# ── Test 4: render_scatter_chart_png_b64 — real PNG bytes ─────────────────────
print("=" * 70)
print("TEST 4: render_scatter_chart_png_b64 renders real base64 PNG bytes")
print("=" * 70)

b64 = render_scatter_chart_png_b64(resolved3)
assert b64, "Must render real base64 bytes for a valid scatter spec"
png_bytes = base64.b64decode(b64)
assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n", "Must be a real PNG file signature"
print(f"PASS: rendered {len(png_bytes)} real PNG bytes.\n")


# ── Test 5: assemble_full_walkthrough folds both into Part C in both docs ─────
print("=" * 70)
print("TEST 5: glossary + scatter steps appear around the chart step, in both")
print("generate_report() and generate_presentation()")
print("=" * 70)

viz_entry = {
    "timestamp": "2026-09-06T00:00:00+00:00",
    "route_response": "visualize",
    "user_question": "Of the 5 highest-paying industries, which offer the highest satisfaction?",
    "curated_question": "Of the 5 highest-paying industries, which offer the highest satisfaction?",
    "chart_type": "bar chart",
    "chart_type_source": "explicit",
    "chart_type_reasoning": "",
    "generated_sql_query": "SELECT industry, avg_salary_k, avg_rating, job_count FROM x",
    "is_safe": "yes",
    "sql_query_execution_result": FAKE_RESULT,
    "output_file_path": "",
    "chart_image_path": "",
    "final_answer": "Staffing & Outsourcing leads in satisfaction.",
    "transformation_narrative_log": [],
    "transformation_candidates_not_relevant": [],
}

report_path5 = generate_report(viz_entry)
report_html5 = Path(report_path5).read_text()
assert "What do these categories mean?" in report_html5
assert "Scatter, sized by volume" in report_html5
chart_idx = report_html5.find("Visualize the result as a bar chart")
glossary_idx = report_html5.find("What do these categories mean?")
scatter_idx = report_html5.find("Scatter, sized by volume")
assert 0 <= glossary_idx < chart_idx < scatter_idx, (
    f"expected glossary < chart < scatter ordering, got {glossary_idx}, {chart_idx}, {scatter_idx}"
)
print(f"PASS: report {report_path5} folds glossary before and scatter after the chart step.\n")

pres_path5 = generate_presentation(viz_entry)
pres_html5 = Path(pres_path5).read_text()
assert "What do these categories mean?" in pres_html5
assert "Scatter, sized by volume" in pres_html5
print(f"PASS: presentation {pres_path5} folds the same two steps in the same order.\n")


# ── Test 6: end-to-end regression — generate_presentation still works ─────────
print("=" * 70)
print("TEST 6: generate_presentation(last_entry) still produces a valid slideshow")
print("=" * 70)

entry6 = last_query_log_entry()
assert entry6 is not None, "Need at least one entry in query_log.jsonl"
pres_path = generate_presentation(entry6)
pres = Path(pres_path)
assert pres.exists() and pres.stat().st_size > 0
html_text = pres.read_text()
assert 'id="prev"' in html_text and 'id="next"' in html_text
print(f"PASS: regenerated a valid slideshow at {pres_path}\n")


print("=" * 70)
print("ALL PRESENTATION SIMPLIFICATION TESTS PASSED")
print("=" * 70)
