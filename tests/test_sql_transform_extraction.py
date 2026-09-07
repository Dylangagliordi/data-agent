"""Tests for utils/sql_transform_extraction.py and its wiring into the
shared narrative walkthrough (utils/narrative.py:_add_question_shaping_step,
Spec 2) that both utils/generate_report.py and utils/generate_presentation.py
render from.

Test 1: extract_question_transformations on the real multi-CTE
        highest-paying-industries query — every field parsed correctly.
Test 2: has_any_transformation is False for a plain passthrough query with
        no computed columns, grouping, having, ranking limit, or scope filter.
Test 3: _extract_scope_filters excludes NULL-check/placeholder-exclusion
        noise that belongs to the general cleaning story, not this one.
Test 4: build_narrative_walkthrough's "Shape the data for this question" step
        reflects the real metrics/grouping/threshold for a shaping-rich
        query, and states honestly that no shaping was needed for a plain
        passthrough query — and generate_report() renders that step inside
        Part C: Analysis.
Test 5: generate_presentation() renders the exact same step as a slide.
"""

import json
from pathlib import Path

from utils.sql_transform_extraction import (
    extract_question_transformations,
    has_any_transformation,
)
from utils.generate_report import generate_report
from utils.generate_presentation import generate_presentation
from utils.narrative import build_narrative_walkthrough

SHAPING_SQL = """WITH base AS (
  SELECT
    industry,
    salary_min_k,
    salary_max_k,
    rating
  FROM uncleaned_ds_jobs
  WHERE job_title ILIKE '%data scientist%'
    AND industry IS NOT NULL
    AND salary_min_k IS NOT NULL
    AND salary_max_k IS NOT NULL
    AND rating IS NOT NULL
),
industry_stats AS (
  SELECT
    industry,
    AVG((salary_min_k + salary_max_k) / 2.0) AS avg_salary_k,
    AVG(rating) AS avg_satisfaction_rating,
    COUNT(*) AS job_count
  FROM base
  GROUP BY industry
  HAVING COUNT(*) >= 5
),
top5_highest_paying AS (
  SELECT *
  FROM industry_stats
  ORDER BY avg_salary_k DESC
  LIMIT 5
)
SELECT
  industry,
  avg_salary_k,
  avg_satisfaction_rating,
  job_count
FROM top5_highest_paying
ORDER BY avg_satisfaction_rating DESC, avg_salary_k DESC;"""

PLAIN_SQL = (
    "SELECT customer_state, order_id FROM orders "
    "WHERE customer_state IS NOT NULL AND order_id <> '-1'"
)

FAKE_RESULT = json.dumps({
    "columns": ["industry", "avg_salary_k", "avg_satisfaction_rating", "job_count"],
    "rows": [
        ["Staffing & Outsourcing", 128.97, 4.14, 35],
        ["Federal Agencies", 134.59, 3.96, 11],
    ],
    "truncated": False,
})

# ── Test 1: full extraction on the real multi-CTE query ────────────────────
print("=" * 70)
print("TEST 1: extract_question_transformations on real multi-CTE query")
print("=" * 70)

t = extract_question_transformations(SHAPING_SQL)
assert t["cte_steps"] == ["base", "industry_stats", "top5_highest_paying"], t["cte_steps"]
assert {c["alias"] for c in t["computed_columns"]} == {
    "avg_salary_k", "avg_satisfaction_rating", "job_count",
}, t["computed_columns"]
assert t["grouping_columns"] == [["industry"]], t["grouping_columns"]
assert t["having_threshold"] == 5, t["having_threshold"]
assert len(t["ranking_stages"]) == 2, t["ranking_stages"]
assert t["ranking_stages"][0]["limit"] == 5, t["ranking_stages"][0]
assert t["ranking_stages"][1]["limit"] is None, t["ranking_stages"][1]
assert any("data scientist" in f for f in t["scope_filters"]), t["scope_filters"]
assert has_any_transformation(t) is True
print("PASS: every field extracted correctly.\n")

# ── Test 2: plain passthrough query has no transformations ─────────────────
print("=" * 70)
print("TEST 2: plain passthrough query -> has_any_transformation is False")
print("=" * 70)

t2 = extract_question_transformations(PLAIN_SQL)
assert t2["computed_columns"] == [], t2["computed_columns"]
assert t2["grouping_columns"] == [], t2["grouping_columns"]
assert t2["having_threshold"] is None
assert t2["ranking_stages"] == []
# NULL checks and placeholder exclusions are general-cleaning noise, not
# question-specific scope filters -> must not leak into scope_filters.
assert t2["scope_filters"] == [], t2["scope_filters"]
assert has_any_transformation(t2) is False
print("PASS: plain query correctly yields no question-specific shaping.\n")

# ── Test 3: scope filters exclude NULL-check / placeholder noise ───────────
print("=" * 70)
print("TEST 3: scope filters exclude IS NOT NULL / placeholder-exclusion noise")
print("=" * 70)

mixed_sql = (
    "SELECT industry FROM t WHERE industry IS NOT NULL "
    "AND industry <> '-1' AND job_title ILIKE '%data scientist%'"
)
t3 = extract_question_transformations(mixed_sql)
assert len(t3["scope_filters"]) == 1, t3["scope_filters"]
assert "data scientist" in t3["scope_filters"][0]
print(f"PASS: only the real scope filter surfaced: {t3['scope_filters']}\n")

# ── Test 4: the shared narrative walkthrough's shaping step is correct ─────
print("=" * 70)
print("TEST 4: 'Shape the data for this question' narrative step is correct")
print("=" * 70)

shaping_entry = {"generated_sql_query": SHAPING_SQL, "final_answer": "", "chart_type": "",
                  "transformation_narrative_log": [], "transformation_candidates_not_relevant": []}
steps_shaping = build_narrative_walkthrough(shaping_entry)
shaping_step = next(s for s in steps_shaping if s.title == "Shape the data for this question")
assert "industry" in shaping_step.explanation.lower()
assert "AVG((salary_min_k + salary_max_k) / 2.0)" in shaping_step.technical_detail
assert shaping_step.stats["grouping_columns"] == [["industry"]]
assert shaping_step.stats["having_threshold"] == 5
assert "grouped the data by industry" in shaping_step.explanation.lower()
assert "fewer than 5" in shaping_step.explanation
print("PASS: shaping step produces real, grounded content.\n")

plain_entry = {"generated_sql_query": PLAIN_SQL, "final_answer": "", "chart_type": "",
                "transformation_narrative_log": [], "transformation_candidates_not_relevant": []}
steps_plain = build_narrative_walkthrough(plain_entry)
plain_step = next(s for s in steps_plain if s.title == "Shape the data for this question")
assert "no extra grouping" in plain_step.explanation.lower() or "used the source data directly" in plain_step.explanation.lower()
print("PASS: plain-query step states honestly that no shaping was needed.\n")

viz_entry = {
    "timestamp": "2026-09-06T00:00:00+00:00",
    "route_response": "visualize",
    "user_question": "Of the 5 highest-paying industries for data scientists, "
                      "which offer the highest employee satisfaction?",
    "curated_question": "Of the 5 highest-paying industries for data scientists, "
                         "which offer the highest employee satisfaction?",
    "chart_type": "bar chart",
    "chart_type_source": "reasoned",
    "chart_type_reasoning": "",
    "generated_sql_query": SHAPING_SQL,
    "is_safe": "yes",
    "sql_query_execution_result": FAKE_RESULT,
    "output_file_path": "",
    "chart_image_path": "",
    "final_answer": "Staffing & Outsourcing leads in satisfaction.",
    "transformation_narrative_log": [],
    "transformation_candidates_not_relevant": [],
}
report_path = generate_report(viz_entry)
report_html = Path(report_path).read_text()
assert "Shape the data for this question" in report_html
assert "industry" in report_html
# The shaping step must appear inside Part C: Analysis, after Part A.
cleaning_idx = report_html.find("Part A: Data Cleaning")
shaping_idx = report_html.find("Shape the data for this question")
analysis_idx = report_html.find("Part C: Analysis")
assert 0 <= cleaning_idx < analysis_idx < shaping_idx, (
    f"expected order Part A < Part C < shaping step, "
    f"got {cleaning_idx}, {analysis_idx}, {shaping_idx}"
)
print(f"PASS: report {report_path} has the shaping step in the right position.\n")

# ── Test 5: generate_presentation embeds/omits the shaping slide correctly ─
print("=" * 70)
print("TEST 5: generate_presentation() embeds/omits the shaping slide")
print("=" * 70)

pres_path = generate_presentation(viz_entry)
pres_html = Path(pres_path).read_text()
assert "Shape the data for this question" in pres_html
assert "industry" in pres_html
print(f"PASS: presentation {pres_path} embeds the shaping slide.\n")

plain_viz_entry = dict(viz_entry, generated_sql_query=PLAIN_SQL)
pres_path_plain = generate_presentation(plain_viz_entry)
pres_html_plain = Path(pres_path_plain).read_text()
assert "Shape the data for this question" in pres_html_plain
assert "used the source data directly" in pres_html_plain.lower()
print(f"PASS: presentation {pres_path_plain} states honestly that no shaping was needed "
      "for a plain passthrough query (the step is still present, per Spec 2's "
      "'render every step, no omission' rule — it just says nothing extra happened).\n")

print("=" * 70)
print("ALL SQL_TRANSFORM_EXTRACTION TESTS PASSED")
print("=" * 70)
