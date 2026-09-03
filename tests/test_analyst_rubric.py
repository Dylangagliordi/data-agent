"""Unit tests for the Analyst Judgment Rubric helpers in agents/sql_analyst.py.

All 6 tests are pure-unit (no LLM, no DB) — they exercise the deterministic
extraction and disclosure functions only.

Test 1: HAVING threshold → disclosure mentions the real threshold value.
Test 2: Outlier sensitivity → note fires when one group's metric > 3× median.
Test 3: NULL exclusion → _extract_null_exclusion_disclosures returns correct strings.
Test 4: Combined-metric ORDER BY → both columns appear in the disclosure.
Test 5: _rubric_applicable_instructions → returns non-empty for relevant questions.
Test 6: No disclosure for a plain COUNT query with no judgment calls.
"""

import re

from agents.sql_analyst import (
    _analyst_judgment_disclosure,
    _apply_causal_correction,
    _extract_null_exclusion_disclosures,
    _extract_time_framing_disclosure,
    _group_size_imbalance_note,
    _outlier_sensitivity_note,
    _rubric_applicable_instructions,
)

# ── Test 1: HAVING threshold ──────────────────────────────────────────────────
print("=" * 70)
print("TEST 1: HAVING threshold → _analyst_judgment_disclosure mentions threshold")
print("=" * 70)

sql_having_5 = """
SELECT industry, AVG(salary_estimate) AS avg_salary, AVG(job_satisfaction) AS avg_satisfaction
FROM data_science_jobs
GROUP BY industry
HAVING COUNT(*) >= 5
ORDER BY avg_salary DESC, avg_satisfaction DESC;
"""
d = _analyst_judgment_disclosure(sql_having_5)
print(f"Disclosure: {d!r}")
assert "5" in d, f"Must mention threshold 5, got: {d!r}"
assert d != "", "Must produce a non-empty disclosure for a HAVING query"
print("PASS: threshold 5 mentioned in disclosure.\n")

sql_having_10 = """
SELECT category, AVG(price) AS avg_price
FROM products
GROUP BY category
HAVING COUNT(*) >= 10
ORDER BY avg_price DESC;
"""
d10 = _analyst_judgment_disclosure(sql_having_10)
assert "10" in d10, f"Must mention threshold 10, got: {d10!r}"
print("PASS: threshold 10 correctly extracted from a different query.\n")


# ── Test 2: Outlier sensitivity ───────────────────────────────────────────────
print("=" * 70)
print("TEST 2: outlier sensitivity → note fires when max > 3× median")
print("=" * 70)

# max value (100) is 10× median (10) — should trigger
outlier_data = [
    {"category": "A", "avg_salary": 10.0},
    {"category": "B", "avg_salary": 12.0},
    {"category": "C", "avg_salary": 11.0},
    {"category": "D", "avg_salary": 100.0},  # outlier
    {"category": "E", "avg_salary": 9.0},
]
note = _outlier_sensitivity_note(outlier_data)
print(f"Outlier note: {note!r}")
assert note != "", "Should produce an outlier note when max > 3× median"
assert "avg salary" in note.lower() or "Avg Salary" in note, (
    f"Note should mention the metric column, got: {note!r}"
)
print("PASS: outlier note fires correctly.\n")

# Values are similar — should NOT trigger
normal_data = [
    {"category": "A", "avg_salary": 10.0},
    {"category": "B", "avg_salary": 12.0},
    {"category": "C", "avg_salary": 11.0},
    {"category": "D", "avg_salary": 13.0},
]
note_none = _outlier_sensitivity_note(normal_data)
assert note_none == "", f"Should not fire for similar values, got: {note_none!r}"
print("PASS: no outlier note when values are similar.\n")

# Only 2 rows — below minimum for outlier detection
tiny_data = [{"category": "A", "avg_val": 1.0}, {"category": "B", "avg_val": 100.0}]
assert _outlier_sensitivity_note(tiny_data) == "", "Under 3 rows — should not fire"
print("PASS: no outlier note for fewer than 3 rows.\n")


# ── Test 3: NULL exclusion ────────────────────────────────────────────────────
print("=" * 70)
print("TEST 3: NULL exclusion → _extract_null_exclusion_disclosures")
print("=" * 70)

sql_null = """
SELECT industry, AVG(salary_estimate) AS avg_salary
FROM data_science_jobs
WHERE salary_estimate IS NOT NULL
GROUP BY industry;
"""
disclosures = _extract_null_exclusion_disclosures(sql_null)
print(f"Disclosures: {disclosures}")
assert len(disclosures) == 1, f"Expected 1 disclosure, got {len(disclosures)}: {disclosures}"
assert "salary" in disclosures[0].lower(), (
    f"Disclosure should mention 'salary', got: {disclosures[0]!r}"
)
print(f"PASS: null exclusion disclosure: {disclosures[0]!r}\n")

# Two different columns with IS NOT NULL
sql_two_nulls = """
SELECT city, AVG(price) FROM listings
WHERE price IS NOT NULL AND rating IS NOT NULL
GROUP BY city;
"""
d2 = _extract_null_exclusion_disclosures(sql_two_nulls)
assert len(d2) == 2, f"Expected 2 disclosures, got {len(d2)}: {d2}"
cols_mentioned = " ".join(d2).lower()
assert "price" in cols_mentioned and "rating" in cols_mentioned, (
    f"Both columns should appear, got: {d2}"
)
print("PASS: two null exclusions → two disclosures.\n")

# No IS NOT NULL in query
plain_sql = "SELECT COUNT(*) AS total_orders FROM orders;"
assert _extract_null_exclusion_disclosures(plain_sql) == [], (
    "Plain query with no IS NOT NULL should return []"
)
print("PASS: no null exclusion disclosures for a plain query.\n")

# Duplicate column same query — deduplication
sql_dup = "SELECT a FROM t WHERE col IS NOT NULL AND col IS NOT NULL;"
d_dup = _extract_null_exclusion_disclosures(sql_dup)
assert len(d_dup) == 1, f"Should deduplicate same column, got {len(d_dup)}: {d_dup}"
print("PASS: duplicate IS NOT NULL on same column is deduplicated.\n")


# ── Test 4: Combined-metric ORDER BY ─────────────────────────────────────────
print("=" * 70)
print("TEST 4: combined ORDER BY → both columns appear in disclosure")
print("=" * 70)

sql_multi_order = """
SELECT industry, AVG(low_salary) AS avg_salary_estimate, AVG(rating) AS avg_job_satisfaction
FROM uncleaned_ds_jobs
GROUP BY industry
HAVING COUNT(*) >= 5
ORDER BY avg_salary_estimate DESC, avg_job_satisfaction DESC;
"""
d_multi = _analyst_judgment_disclosure(sql_multi_order)
print(f"Disclosure: {d_multi!r}")

assert "avg salary estimate" in d_multi.lower() or "Avg Salary Estimate" in d_multi, (
    f"Primary sort column must appear, got: {d_multi!r}"
)
assert "avg job satisfaction" in d_multi.lower() or "Avg Job Satisfaction" in d_multi, (
    f"Secondary sort column must appear, got: {d_multi!r}"
)
assert "primarily" in d_multi.lower(), "Disclosure should use 'primarily' for primary sort"
print("PASS: combined ORDER BY → both columns disclosed.\n")

# Single-column ORDER BY — no multi-metric disclosure
sql_single_order = "SELECT category, COUNT(*) AS n FROM t GROUP BY category ORDER BY n DESC;"
d_single = _analyst_judgment_disclosure(sql_single_order)
assert "primarily" not in d_single.lower(), (
    f"Single ORDER BY column should not produce a combined-ranking disclosure, got: {d_single!r}"
)
print("PASS: single-column ORDER BY produces no combined-ranking disclosure.\n")


# ── Test 5: _rubric_applicable_instructions ───────────────────────────────────
print("=" * 70)
print("TEST 5: _rubric_applicable_instructions → non-empty for relevant questions")
print("=" * 70)

# Avg/rate comparison question
q_avg = "Which industry has the highest average salary?"
inst_avg = _rubric_applicable_instructions(q_avg)
print(f"Instructions for avg question: {inst_avg!r}")
assert inst_avg != "", "Should return notes for an 'average/highest' question"
assert "RUBRIC NOTE" in inst_avg, "Notes should be labelled RUBRIC NOTE"
print("PASS: instructions returned for average/highest question.\n")

# Causal question
q_causal = "Why do customers in São Paulo spend more?"
inst_causal = _rubric_applicable_instructions(q_causal)
assert inst_causal != "", "Should return causal note for 'why' question"
assert "causal" in inst_causal.lower() or "association" in inst_causal.lower(), (
    f"Causal note should mention causation/association, got: {inst_causal!r}"
)
print("PASS: causal note returned for 'why' question.\n")

# Simple count question — no rubric instructions expected
q_plain = "How many orders are there?"
inst_plain = _rubric_applicable_instructions(q_plain)
assert inst_plain == "", (
    f"Simple count question should produce no rubric instructions, got: {inst_plain!r}"
)
print("PASS: no instructions for a simple count question.\n")


# ── Test 6: No disclosure for a simple query ──────────────────────────────────
print("=" * 70)
print("TEST 6: no disclosure for a plain COUNT query with no judgment calls")
print("=" * 70)

sql_plain = "SELECT COUNT(*) AS total_orders FROM olist_orders_dataset;"
d_plain = _analyst_judgment_disclosure(sql_plain)
print(f"Disclosure: {d_plain!r}")
assert d_plain == "", (
    f"Must NOT fabricate a disclosure for a plain COUNT query, got: {d_plain!r}"
)
print("PASS: no disclosure for a plain COUNT query.\n")

# Also check that a GROUP BY without HAVING and single ORDER BY → empty
sql_simple_group = """
SELECT customer_state, COUNT(*) AS order_count
FROM olist_orders_dataset
GROUP BY customer_state
ORDER BY order_count DESC
LIMIT 10;
"""
d_simple = _analyst_judgment_disclosure(sql_simple_group)
assert d_simple == "", (
    f"Single ORDER BY with no HAVING → no disclosure, got: {d_simple!r}"
)
print("PASS: simple GROUP BY / single ORDER BY → no disclosure.\n")


# ── Bonus: _apply_causal_correction ──────────────────────────────────────────
print("=" * 70)
print("BONUS: _apply_causal_correction → appends disclaimer for causal language")
print("=" * 70)

causal_answer = "Higher salaries lead to better job satisfaction in this dataset."
corrected = _apply_causal_correction(causal_answer, "SELECT 1")
print(f"Corrected answer: {corrected!r}")
assert "statistical association" in corrected.lower(), (
    f"Causal answer should get an association disclaimer, got: {corrected!r}"
)
print("PASS: causal answer gets the association disclaimer.\n")

plain_answer = "The average salary across industries is $95,000."
unchanged = _apply_causal_correction(plain_answer, "SELECT 1")
assert unchanged == plain_answer, (
    f"Non-causal answer should be unchanged, got: {unchanged!r}"
)
print("PASS: non-causal answer is left unchanged.\n")

# Idempotent — second correction doesn't duplicate the disclaimer
corrected_twice = _apply_causal_correction(corrected, "SELECT 1")
from agents.sql_analyst import _ASSOCIATION_DISCLAIMER
count = corrected_twice.count(_ASSOCIATION_DISCLAIMER)
assert count == 1, f"Disclaimer should appear exactly once, got {count} times"
print("PASS: _apply_causal_correction is idempotent.\n")

# Time framing extraction
sql_between = """
SELECT SUM(payment_value) FROM payments
WHERE order_purchase_timestamp BETWEEN '2017-01-01' AND '2017-12-31';
"""
tf = _extract_time_framing_disclosure(sql_between)
print(f"Time framing: {tf!r}")
assert "2017-01-01" in tf and "2017-12-31" in tf, (
    f"Time framing should mention both dates, got: {tf!r}"
)
print("PASS: BETWEEN date range disclosed in time framing.\n")

# Group size imbalance
imbalanced = [
    {"category": "A", "count": 1, "avg_val": 5.0},
    {"category": "B", "count": 1000, "avg_val": 6.0},
]
imbalance_note = _group_size_imbalance_note(imbalanced)
assert imbalance_note != "", "Should fire for 1000× group size ratio"
assert "1" in imbalance_note and "1000" in imbalance_note, (
    f"Note should mention both min and max counts, got: {imbalance_note!r}"
)
print("PASS: group size imbalance note fires for 1000:1 ratio.\n")

balanced = [
    {"category": "A", "count": 50, "avg_val": 5.0},
    {"category": "B", "count": 55, "avg_val": 6.0},
]
assert _group_size_imbalance_note(balanced) == "", "Should not fire for similar group sizes"
print("PASS: no imbalance note for similarly-sized groups.\n")


print("=" * 70)
print("ALL ANALYST RUBRIC TESTS PASSED")
print("=" * 70)
