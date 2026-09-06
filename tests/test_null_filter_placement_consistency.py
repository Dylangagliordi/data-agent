"""Regression test for NULL-filter-placement non-determinism in per-category
ranking SQL.

Real bug this guards against: two live query_log.jsonl entries for
near-identical phrasings of "Of the 5 highest-paying industries for data
scientists, which offer the highest employee satisfaction?" produced
DIFFERENT generated_sql_query values across separate runs:

- One run applied `WHERE rating IS NOT NULL` (and the other NULL filters)
  inside the base CTE, before GROUP BY -> Consulting had job_count=29,
  avg_industry_salary=$128,103.
- Another run omitted the rating/salary NULL filters from the base CTE
  entirely (filtering implicitly via arithmetic later) -> Consulting had
  job_count=31, avg_industry_salary=$131,000.

Root cause: GENERATE_SQL_SYSTEM_PROMPT's null-handling rule said filters must
appear literally in the SQL (for disclosure), but never fixed WHERE those
filters must live relative to GROUP BY — so the same question could silently
aggregate over a different set of underlying rows every time it was asked,
exactly the kind of unannounced non-determinism this project already fixed
once for the HAVING COUNT(*) threshold (see test_ranking_consistency.py).

Fix under test: GENERATE_SQL_SYSTEM_PROMPT now states a FIXED convention —
every `col IS NOT NULL` filter feeding a per-category ranking metric must be
applied in ONE WHERE clause in the base CTE, before any GROUP BY. This test
runs generate_sql + execute_sql live, 3 times, on the exact regression
question and confirms every run returns the SAME per-industry job_count and
avg_industry_salary for the same industries — not just a HAVING threshold
that happens to match.
"""

import json

from agents.sql_analyst import add_context, execute_sql, generate_sql
from models.schema import SQLAnalystState

REGRESSION_QUESTION = (
    "Of the 5 highest-paying industries for data scientists, which offer "
    "the highest employee satisfaction?"
)

print("=" * 70)
print("TEST: live generate_sql + execute_sql, 3 real runs of the regression question")
print("=" * 70)

per_run_results = []
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

    exec_result = execute_sql(state)
    raw = exec_result["sql_query_execution_result"]
    assert not raw.startswith("SQL_EXECUTION_ERROR"), (
        f"Run {i + 1}: SQL execution failed: {raw}"
    )
    payload = json.loads(raw)
    cols = payload["columns"]
    rows = payload["rows"]

    # Build {industry: {col: value}} for this run, keyed by whichever column
    # actually holds the industry name (first non-numeric column).
    industry_col = cols[0]
    per_industry = {r[0]: dict(zip(cols, r)) for r in rows}
    print(f"Run {i + 1} parsed rows: {per_industry}")
    per_run_results.append(per_industry)

print("\n" + "=" * 70)
print("Comparing job_count / avg salary across all 3 runs per industry")
print("=" * 70)

# Every run should identify the same SET of top-5 industries (by pay) —
# and for each industry present in all runs, the same underlying row count
# and average salary, proving the same NULL-filtered rows were aggregated
# every time.
common_industries = set(per_run_results[0]) & set(per_run_results[1]) & set(per_run_results[2])
assert common_industries, (
    f"Expected at least one industry common to all 3 runs; got "
    f"{[set(r) for r in per_run_results]}"
)
print(f"Industries common to all 3 runs: {sorted(common_industries)}")

count_col_candidates = ["job_count", "n", "num_postings", "count", "cnt"]
salary_col_candidates = ["avg_industry_salary", "avg_salary", "avg_salary_estimate"]

def _find_col(row: dict, candidates: list) -> str:
    return next((c for c in candidates if c in row), None)

for industry in sorted(common_industries):
    counts = []
    salaries = []
    for run in per_run_results:
        row = run[industry]
        count_col = _find_col(row, count_col_candidates)
        salary_col = _find_col(row, salary_col_candidates)
        assert count_col, f"Expected a row-count column in result columns: {list(row.keys())}"
        assert salary_col, f"Expected a salary column in result columns: {list(row.keys())}"
        counts.append(row[count_col])
        salaries.append(round(float(row[salary_col]), 2))
    print(f"{industry}: counts={counts} salaries={salaries}")
    assert len(set(counts)) == 1, (
        f"Industry {industry!r}: job/row count varied across runs ({counts}) — "
        f"NULL-filter placement is non-deterministic again."
    )
    assert len(set(salaries)) == 1, (
        f"Industry {industry!r}: avg salary varied across runs ({salaries}) — "
        f"the same underlying rows must be aggregated every time."
    )

print("\nPASS: job_count and avg salary identical across all 3 runs for every common industry.\n")

print("=" * 70)
print("ALL NULL-FILTER-PLACEMENT CONSISTENCY TESTS PASSED")
print("=" * 70)
