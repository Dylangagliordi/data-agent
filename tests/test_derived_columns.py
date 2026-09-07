"""
Spec 1, Part 7 (remaining requirement): derived columns are tagged as
"derived, not source" (utils.load_data._derived_columns) so generate_sql and
disclosure logic never mistake a computed value for an observed one.

Test 1: mark_derived_columns / get_derived_columns round-trip through real
Postgres.
Test 2: add_context's schema context annotates a marked column with
"[DERIVED via <kind> — not an observed source value]" right in the column
line generate_sql reads, while a genuinely observed column in the same table
is annotated with nothing extra.
Test 3: _derived_columns itself is excluded from add_context's
information_schema query (same discipline as _data_quality_status/
_fanout_status/_transformation_candidates/_transformation_decisions) — it
must never appear as an ordinary queryable table.

Requires a live Postgres (same as test_fanout_status.py), no LLM.
"""

from agents.sql_analyst import add_context
from models.schema import SQLAnalystState
from utils.load_data import (
    ensure_data_quality_status_table,
    ensure_derived_columns_table,
    ensure_fanout_status_table,
    get_admin_connection,
    get_derived_columns,
    mark_derived_columns,
    write_data_quality_status,
)


def cleanup(conn, *table_names: str) -> None:
    with conn.cursor() as cur:
        for t in table_names:
            cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE;')
            cur.execute("DELETE FROM _derived_columns WHERE table_name = %s", (t,))
            cur.execute("DELETE FROM _data_quality_status WHERE table_name = %s", (t,))
    conn.commit()


conn = get_admin_connection()
ensure_derived_columns_table(conn)
ensure_data_quality_status_table(conn)
ensure_fanout_status_table(conn)

t_jobs = "tdc_jobs"

try:
    cleanup(conn, t_jobs)

    print("=" * 70)
    print("TEST 1: mark_derived_columns / get_derived_columns round trip")
    print("=" * 70)

    mark_derived_columns(conn, t_jobs, ["company_age"], "company_age")
    mark_derived_columns(conn, t_jobs, ["min_salary", "max_salary", "avg_salary"], "range_decomposition")
    derived = get_derived_columns(conn, t_jobs)
    assert derived == {
        "company_age": "company_age",
        "min_salary": "range_decomposition",
        "max_salary": "range_decomposition",
        "avg_salary": "range_decomposition",
    }, derived
    print(f"PASS: {derived}\n")

    print("=" * 70)
    print("TEST 2: add_context annotates a derived column in the real schema")
    print("context generate_sql reads; an observed column is left plain")
    print("=" * 70)

    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{t_jobs}" CASCADE;')
        cur.execute(
            f'CREATE TABLE "{t_jobs}" '
            "(job_title TEXT, founded INTEGER, company_age INTEGER, "
            "min_salary NUMERIC, max_salary NUMERIC, avg_salary NUMERIC);"
        )
        cur.executemany(
            f'INSERT INTO "{t_jobs}" VALUES (%s, %s, %s, %s, %s, %s)',
            [("Data Scientist", 1998, 28, 100, 150, 125)],
        )
    conn.commit()
    write_data_quality_status(conn, t_jobs, "pass", [], False)

    state = SQLAnalystState(
        user_question=f"What is the average salary in {t_jobs}?",
        curated_question=f"What is the average salary in {t_jobs}?",
    )
    ctx = add_context(state)
    ctx_text = ctx["prompt_query_context"]

    company_age_line = next(ln for ln in ctx_text.splitlines() if "company_age (" in ln)
    assert "[DERIVED via company_age" in company_age_line, (
        f"expected company_age annotated as derived, got: {company_age_line}"
    )
    min_salary_line = next(ln for ln in ctx_text.splitlines() if "min_salary (" in ln)
    assert "[DERIVED via range_decomposition" in min_salary_line, (
        f"expected min_salary annotated as derived, got: {min_salary_line}"
    )
    job_title_line = next(ln for ln in ctx_text.splitlines() if "job_title (" in ln)
    assert "DERIVED" not in job_title_line, (
        f"expected job_title (a genuinely observed source column) left unannotated, "
        f"got: {job_title_line}"
    )
    print(f"PASS:\n  {company_age_line}\n  {min_salary_line}\n  {job_title_line}\n")

    print("=" * 70)
    print("TEST 3: _derived_columns itself is excluded from add_context's")
    print("information_schema query — never appears as an ordinary table")
    print("=" * 70)

    assert "_derived_columns" not in ctx_text, (
        "the _derived_columns internal table must never be exposed as a queryable table"
    )
    assert not any(
        "_derived_columns has no recorded data-quality check" in w["warning"]
        for w in ctx["data_quality_warnings"]
    ), "internal table must not trigger a spurious data-quality warning"
    print("PASS: _derived_columns is not exposed as an ordinary table, and triggers no "
          "spurious data-quality warning.\n")

finally:
    cleanup(conn, t_jobs)
    conn.close()

print("=" * 70)
print("ALL DERIVED-COLUMN TAGGING (SPEC 1, PART 7) ASSERTIONS PASSED")
print("=" * 70)
