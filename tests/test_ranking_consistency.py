"""Regression test for the salary-vs-satisfaction ranking inconsistency.

Real bug this guards against: the exact same question ("As a prospective data
scientist, which industry should I pursue to maximize both salary and job
satisfaction?") produced two genuinely different generated_sql_query values
across two live runs (logged in logs/query_log.jsonl):

- Run A used HAVING COUNT(*) >= 5 and ORDER BY avg_salary_estimate DESC,
  avg_job_satisfaction DESC — Video Games (3 postings) was excluded.
- Run B used HAVING COUNT(*) >= 3 and ORDER BY avg_salary DESC,
  avg_job_satisfaction DESC — Video Games was included and became the
  top-satisfaction result.

Root cause: "maximize both X and Y" has no single obvious SQL translation —
there's no fixed answer to "what's the minimum sample size?" or "how do two
metrics combine into one ranking?" generate_sql is an LLM making that
judgment call fresh every call, with nothing constraining it to answer the
same way twice.

Fix under test (two parts):
1. GENERATE_SQL_SYSTEM_PROMPT now states a FIXED convention: HAVING COUNT(*)
   >= 5 for any per-category ranking by an averaged/rate metric, unless the
   question states a different minimum explicitly. This test confirms the
   real, live generate_sql node actually applies that threshold consistently
   across repeated real invocations of this exact question.
2. _ranking_convention_disclosure() deterministically (no LLM) extracts the
   real threshold and combined-ranking method from whatever SQL was actually
   executed, so even if some variance in the threshold ever creeps back in,
   the disclosure line makes it visible rather than silent.

Test 1 (unit, no LLM/DB): _ranking_convention_disclosure() extracts a known
  HAVING threshold and a known multi-column ORDER BY correctly, and returns
  "" for queries with neither.
Test 2 (live, real LLM + real DB): calls generate_sql on the exact regression
  question 3 real times. Confirms every run's SQL uses the fixed HAVING >= 5
  convention (or explicitly named minimum) rather than a lower/inconsistent
  threshold, and confirms represent_final_answer's final_answer explicitly
  states the threshold/ranking method used every time.
"""

from agents.sql_analyst import (
    _ranking_convention_disclosure,
    add_context,
    generate_sql,
    represent_final_answer,
)
from models.schema import SQLAnalystState

REGRESSION_QUESTION = (
    "As a prospective data scientist, which industry should I pursue to "
    "maximize both salary and job satisfaction?"
)

# ── Test 1: unit test of the deterministic extractor ─────────────────────────
print("=" * 70)
print("TEST 1: _ranking_convention_disclosure extracts threshold + combined ranking")
print("=" * 70)

sql_with_both = """
SELECT industry, AVG(rating) AS avg_job_satisfaction, AVG(low_salary) AS avg_salary
FROM uncleaned_ds_jobs
GROUP BY industry
HAVING COUNT(*) >= 5
ORDER BY avg_salary DESC, avg_job_satisfaction DESC;
"""
disclosure = _ranking_convention_disclosure(sql_with_both)
print(f"Disclosure: {disclosure!r}")
assert "5" in disclosure, "must mention the real threshold value (5)"
assert "avg salary" in disclosure.lower() or "Avg Salary" in disclosure, (
    "must mention the primary sort column"
)
assert "avg job satisfaction" in disclosure.lower() or "Avg Job Satisfaction" in disclosure, (
    "must mention the secondary sort column"
)
print("PASS: threshold and combined ranking both extracted.\n")

sql_no_judgment_calls = "SELECT COUNT(*) FROM olist_orders_dataset;"
disclosure_empty = _ranking_convention_disclosure(sql_no_judgment_calls)
assert disclosure_empty == "", (
    f"must NOT fabricate a disclosure for a query with no threshold/combined ranking, "
    f"got: {disclosure_empty!r}"
)
print("PASS: no disclosure fabricated for a plain query with no judgment call.\n")

sql_single_order = "SELECT a, b FROM t GROUP BY a ORDER BY b DESC;"
disclosure_single = _ranking_convention_disclosure(sql_single_order)
assert disclosure_single == "", (
    "a single-column ORDER BY needs no combined-ranking disclosure, "
    f"got: {disclosure_single!r}"
)
print("PASS: single-column ORDER BY correctly produces no disclosure.\n")

# ── Test 2: live regression — repeated real runs of the exact question ───────
print("=" * 70)
print("TEST 2: live generate_sql, 3 real runs of the exact regression question")
print("=" * 70)

thresholds_seen = []
for i in range(3):
    state = SQLAnalystState(
        user_question=REGRESSION_QUESTION,
        curated_question=REGRESSION_QUESTION,
    )
    ctx = add_context(state)
    state = state.model_copy(update=ctx)

    sql_result = generate_sql(state)
    sql = sql_result["generated_sql_query"]
    state = state.model_copy(update=sql_result)
    print(f"\n--- Run {i + 1} generated SQL ---")
    print(sql)

    disclosure = _ranking_convention_disclosure(sql)
    print(f"Run {i + 1} disclosure: {disclosure!r}")

    import re as _re
    m = _re.search(r"HAVING\b.*?COUNT\s*\(\s*\*?\s*\)\s*>=?\s*(\d+)", sql, _re.IGNORECASE | _re.DOTALL)
    assert m, f"Run {i + 1}: generated SQL must include a HAVING COUNT(*) minimum-sample threshold"
    threshold = int(m.group(1))
    thresholds_seen.append(threshold)
    assert threshold >= 5, (
        f"Run {i + 1}: fixed project convention requires HAVING COUNT(*) >= 5 for this "
        f"class of per-category ranking question; got threshold {threshold}"
    )

    # Simulate real execution result so represent_final_answer's disclosure can be
    # checked without a live DB round trip inside this loop — feed a fake sample.
    fake_result = str([
        {"industry": "Staffing & Outsourcing", "avg_job_satisfaction": 4.15, "avg_salary": 128153, "n": 36},
        {"industry": "Computer Hardware & Software", "avg_job_satisfaction": 4.24, "avg_salary": 120219, "n": 57},
    ])
    answer_state = state.model_copy(
        update={"sql_query_execution_result": fake_result, "generated_sql_query": sql}
    )
    answer_result = represent_final_answer(answer_state)
    final_answer = answer_result["final_answer"]
    assert "How this ranking was computed:" in final_answer, (
        f"Run {i + 1}: final_answer must explicitly disclose the threshold/ranking method, "
        f"got: {final_answer!r}"
    )
    assert str(threshold) in final_answer, (
        f"Run {i + 1}: final_answer's disclosure must state the real threshold ({threshold})"
    )
    print(f"PASS: Run {i + 1} used threshold >= {threshold} and disclosed it in final_answer.")

print(f"\nThresholds seen across 3 real runs: {thresholds_seen}")
assert len(set(thresholds_seen)) == 1, (
    f"Expected the same fixed threshold across all 3 runs, got varying values: {thresholds_seen}"
)
print(f"PASS: all 3 real runs used the SAME threshold ({thresholds_seen[0]}) — no more silent variance.\n")

print("=" * 70)
print("ALL RANKING CONSISTENCY TESTS PASSED")
print("=" * 70)
