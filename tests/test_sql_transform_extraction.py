"""Tests for utils/sql_transform_extraction.py and its wiring into
utils/generate_report.py (`_section_question_transformations`) and
utils/generate_presentation.py (`_slide_question_shaping`).

Test 1: extract_question_transformations on the real multi-CTE
        highest-paying-industries query — every field parsed correctly.
Test 2: has_any_transformation is False for a plain passthrough query with
        no computed columns, grouping, having, ranking limit, or scope filter.
Test 3: _extract_scope_filters excludes NULL-check/placeholder-exclusion
        noise that belongs to the general cleaning story, not this one.
Test 4: generate_report() embeds a real "Question-Specific Data Shaping"
        section with the real metrics table for a shaping-rich query, and
        an honest "no shaping" note for a plain passthrough query.
Test 5: generate_presentation() embeds a real "How we shaped the data for
        this answer" slide for the same shaping-rich query, and omits the
        slide entirely for a plain passthrough query.
"""

import json
from pathlib import Path

from utils.sql_transform_extraction import (
    extract_question_transformations,
    has_any_transformation,
)
from utils.generate_report import generate_report, _section_question_transformations
from utils.generate_presentation import generate_presentation, _slide_question_shaping

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

# ── Test 4: generate_report embeds the section correctly ───────────────────
print("=" * 70)
print("TEST 4: generate_report() embeds Question-Specific Data Shaping section")
print("=" * 70)

section_shaping = _section_question_transformations(SHAPING_SQL)
assert "<h2>Question-Specific Data Shaping</h2>" in section_shaping
assert "Avg Salary K" in section_shaping
assert "AVG((salary_min_k + salary_max_k) / 2.0)" in section_shaping
assert "Grouped by:</strong> industry" in section_shaping
assert "fewer than 5" in section_shaping
print("PASS: section builder produces real, grounded content.\n")

section_plain = _section_question_transformations(PLAIN_SQL)
assert "no additional shaping" in section_plain.lower()
print("PASS: plain-query section states honestly that no shaping was needed.\n")

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
}
report_path = generate_report(viz_entry)
report_html = Path(report_path).read_text()
assert "<h2>Question-Specific Data Shaping</h2>" in report_html
assert "Avg Salary K" in report_html
# Section must appear after Data Cleaning and before Topic Focus, in order.
cleaning_idx = report_html.find("<h2>Data Cleaning</h2>")
shaping_idx = report_html.find("<h2>Question-Specific Data Shaping</h2>")
topic_idx = report_html.find("<h2>Topic Focus</h2>")
assert 0 <= cleaning_idx < shaping_idx < topic_idx, (
    f"expected order Data Cleaning < Question-Specific Data Shaping < Topic Focus, "
    f"got {cleaning_idx}, {shaping_idx}, {topic_idx}"
)
print(f"PASS: report {report_path} has the section in the right position.\n")

# ── Test 5: generate_presentation embeds/omits the slide correctly ─────────
print("=" * 70)
print("TEST 5: generate_presentation() embeds/omits the shaping slide")
print("=" * 70)

slide = _slide_question_shaping(SHAPING_SQL)
assert slide is not None
assert "How we shaped the data for this answer" in slide
assert "Avg Salary K" in slide

no_slide = _slide_question_shaping(PLAIN_SQL)
assert no_slide is None, "plain passthrough query must not produce a slide"
print("PASS: slide builder produces content when there's real shaping, "
      "None otherwise.\n")

pres_path = generate_presentation(viz_entry)
pres_html = Path(pres_path).read_text()
assert "How we shaped the data for this answer" in pres_html
assert "Avg Salary K" in pres_html
print(f"PASS: presentation {pres_path} embeds the shaping slide.\n")

print("=" * 70)
print("ALL SQL_TRANSFORM_EXTRACTION TESTS PASSED")
print("=" * 70)
