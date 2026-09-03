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
    _ASSOCIATION_DISCLAIMER,
    _analyst_judgment_disclosure,
    _apply_causal_correction,
    _extract_deduplication_disclosure,
    _extract_null_exclusion_disclosures,
    _extract_time_framing_disclosure,
    _group_size_imbalance_note,
    _outlier_sensitivity_note,
    _rubric_applicable_instructions,
)
from agents.sql_analyst import GENERATE_SQL_SYSTEM_PROMPT

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


# ── Test 7: _apply_causal_correction REWRITES, not just appends ──────────────
print("=" * 70)
print("TEST 7: _apply_causal_correction rewrites causal phrasing in-place")
print("=" * 70)

# "leads to" must be replaced — causal phrase must NOT appear in output
causal_leads = "Higher salaries lead to better job satisfaction in this dataset."
corrected_leads = _apply_causal_correction(causal_leads, "SELECT 1")
print(f"Input:  {causal_leads!r}")
print(f"Output: {corrected_leads!r}")
assert "lead to" not in corrected_leads.lower(), (
    f"'lead to' must be rewritten away, not left in the output: {corrected_leads!r}"
)
assert "is associated with" in corrected_leads, (
    f"Must be replaced with associative language, got: {corrected_leads!r}"
)
# Disclaimer should NOT be needed when the phrase was cleanly rewritten
assert _ASSOCIATION_DISCLAIMER not in corrected_leads, (
    "No disclaimer needed when causal phrase was already rewritten"
)
print("PASS: 'leads to' rewritten to 'is associated with'; no stray disclaimer.\n")

# "causes" must be replaced
causal_causes = "Increased competition causes prices to drop."
corrected_causes = _apply_causal_correction(causal_causes, "SELECT 1")
print(f"Output: {corrected_causes!r}")
assert "causes" not in corrected_causes.lower(), (
    f"'causes' must be rewritten away: {corrected_causes!r}"
)
assert "is associated with" in corrected_causes, (
    f"Must be replaced with associative language, got: {corrected_causes!r}"
)
print("PASS: 'causes' rewritten to 'is associated with'.\n")

# "because of" → "alongside"
causal_because = "Revenue is high because of strong demand in São Paulo."
corrected_because = _apply_causal_correction(causal_because, "SELECT 1")
print(f"Output: {corrected_because!r}")
assert "because of" not in corrected_because.lower(), (
    f"'because of' must be rewritten: {corrected_because!r}"
)
assert "alongside" in corrected_because, (
    f"Must be replaced with 'alongside', got: {corrected_because!r}"
)
print("PASS: 'because of' rewritten to 'alongside'.\n")

# Ambiguous verb "drives" triggers disclaimer fallback (not rewritten)
ambiguous_drives = "Customer volume drives total revenue in this analysis."
corrected_drives = _apply_causal_correction(ambiguous_drives, "SELECT 1")
print(f"Output: {corrected_drives!r}")
# "drives" stays in the text (ambiguous, no substitution)
assert "drives" in corrected_drives.lower(), (
    f"Ambiguous 'drives' should remain in text (disclaimer fallback), got: {corrected_drives!r}"
)
assert _ASSOCIATION_DISCLAIMER in corrected_drives, (
    f"Ambiguous causal verb should trigger the disclaimer fallback, got: {corrected_drives!r}"
)
print("PASS: ambiguous 'drives' triggers disclaimer fallback without rewriting.\n")

# Non-causal answer is left completely unchanged
plain = "The average salary across industries is $95,000."
assert _apply_causal_correction(plain, "SELECT 1") == plain, (
    "Non-causal answer must be unchanged"
)
print("PASS: non-causal answer left unchanged.\n")

# Idempotent — applying twice doesn't duplicate the disclaimer
corrected_twice = _apply_causal_correction(corrected_drives, "SELECT 1")
count = corrected_twice.count(_ASSOCIATION_DISCLAIMER)
assert count == 1, f"Disclaimer should appear exactly once, got {count} times"
print("PASS: _apply_causal_correction is idempotent.\n")


# ── Test 8: duplicate-row disclosure (_extract_deduplication_disclosure) ──────
print("=" * 70)
print("TEST 8: SELECT DISTINCT → deduplication disclosure fires")
print("=" * 70)

sql_distinct = """
SELECT DISTINCT customer_id, order_status
FROM olist_orders_dataset
WHERE order_status = 'delivered';
"""
dedup = _extract_deduplication_disclosure(sql_distinct)
print(f"Disclosure: {dedup!r}")
assert dedup != "", "SELECT DISTINCT must trigger deduplication disclosure"
assert "duplicate" in dedup.lower(), f"Must mention 'duplicate', got: {dedup!r}"
assert "repeated" in dedup.lower() or "observations" in dedup.lower(), (
    f"Must mention 'repeated observations', got: {dedup!r}"
)
print("PASS: SELECT DISTINCT triggers deduplication disclosure.\n")

# No DISTINCT → no disclosure
sql_no_distinct = "SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id;"
assert _extract_deduplication_disclosure(sql_no_distinct) == "", (
    "Query without SELECT DISTINCT should produce no deduplication disclosure"
)
print("PASS: no deduplication disclosure when SELECT DISTINCT is absent.\n")

# _analyst_judgment_disclosure integrates it
d_distinct = _analyst_judgment_disclosure(sql_distinct)
assert "duplicate" in d_distinct.lower(), (
    f"_analyst_judgment_disclosure must surface the dedup note, got: {d_distinct!r}"
)
print("PASS: _analyst_judgment_disclosure surfaces deduplication note.\n")

# DISTINCT in subquery also triggers (it's still in the SQL text)
sql_subq_distinct = """
SELECT city, total_orders FROM (
    SELECT DISTINCT customer_city AS city, COUNT(*) AS total_orders
    FROM customers GROUP BY customer_city
) sub;
"""
assert _extract_deduplication_disclosure(sql_subq_distinct) != "", (
    "DISTINCT inside subquery should also trigger"
)
print("PASS: DISTINCT inside subquery also triggers.\n")


# ── Test 9: composite-value splitting rule in system prompt ───────────────────
print("=" * 70)
print("TEST 9: composite-value splitting rule present in GENERATE_SQL_SYSTEM_PROMPT")
print("=" * 70)

assert "SPLIT_PART" in GENERATE_SQL_SYSTEM_PROMPT or "split_part" in GENERATE_SQL_SYSTEM_PROMPT.lower(), (
    "System prompt must reference SPLIT_PART for composite-value splitting"
)
assert "Composite" in GENERATE_SQL_SYSTEM_PROMPT or "composite" in GENERATE_SQL_SYSTEM_PROMPT.lower(), (
    "System prompt must have a composite-value rule"
)
assert "REGEXP_REPLACE" in GENERATE_SQL_SYSTEM_PROMPT or "regexp_replace" in GENERATE_SQL_SYSTEM_PROMPT.lower(), (
    "System prompt must reference REGEXP_REPLACE as a splitting tool"
)
print("PASS: composite-value splitting rule present in GENERATE_SQL_SYSTEM_PROMPT.\n")


# ── Ancillary: time framing + group size (proving existing helpers still work) ─
print("=" * 70)
print("ANCILLARY: time framing + group size imbalance proofs")
print("=" * 70)

sql_between = """
SELECT SUM(payment_value) FROM payments
WHERE order_purchase_timestamp BETWEEN '2017-01-01' AND '2017-12-31';
"""
tf = _extract_time_framing_disclosure(sql_between)
assert "2017-01-01" in tf and "2017-12-31" in tf, f"Time framing failed: {tf!r}"
print("PASS: BETWEEN date range disclosed.\n")

imbalanced = [
    {"category": "A", "count": 1, "avg_val": 5.0},
    {"category": "B", "count": 1000, "avg_val": 6.0},
]
imbalance_note = _group_size_imbalance_note(imbalanced)
assert imbalance_note != "", "Should fire for 1000× group size ratio"
assert "1000" in imbalance_note, f"Should mention max count 1000: {imbalance_note!r}"
print("PASS: group size imbalance fires for 1000:1 ratio.\n")

balanced = [{"category": "A", "count": 50, "avg_val": 5.0}, {"category": "B", "count": 55}]
assert _group_size_imbalance_note(balanced) == "", "Should not fire for similar group sizes"
print("PASS: no imbalance note for similar group sizes.\n")


print("=" * 70)
print("ALL ANALYST RUBRIC TESTS PASSED")
print("=" * 70)
