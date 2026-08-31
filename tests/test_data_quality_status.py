"""Standalone test for persistent, per-table data-quality tracking:
_data_quality_status (written by utils/load_data.py) and the two places that read it
— agents/sql_analyst.py's add_context (schema-context WARNING injection) and
represent_final_answer (final-answer note, fail-level/no-record only).

Uses real Postgres (the same app_reader / admin connections the rest of the project
uses) and real fixture folders under data/_test_etl/. Each scenario loads its table
directly via utils.load_data.load_csv_to_table + the same status-writing helpers
main() calls, rather than shelling out to the whole load_data.py CLI, so this test can
inject a fake (deterministic, never-succeeds) LLM into clean_dataset() for the
fail-level scenario without depending on the real model happening to write broken code.

Four scenarios (SEVERITY MAPPING reused throughout from utils.data_cleaning's own
FAIL_LEVEL_PREFIXES/WARN_LEVEL_PREFIXES — never redefined here):

1. Clean table (no issues at all): status row is "pass"; a question against it
   produces NO data-quality warning in the final answer.
2. Warn-only table (formatting noise, a WARN_LEVEL_PREFIXES category): status row is
   "warn"; a question against it does NOT surface a warning in the final answer (warn
   is real but not serious enough to affect generate_sql's context OR the answer).
3. Fail-level table (duplicate values/rows, FAIL_LEVEL_PREFIXES categories) that a
   deliberately-broken fake LLM never manages to fix: status row is "fail"; a question
   against it DOES surface a clear warning in the final answer.
4. No-status-row table: inserted directly into Postgres bypassing load_data.py
   entirely (simulating a table loaded before this system existed). add_context must
   inject the "never checked" WARNING, and the final answer must mention it.

Run with (the piped 'yes' answers approve the deterministic fake-LLM-generated cleaning
attempts for the warn-only and fail-level fixtures above — 3 for the warn batch's own
MAX_CLEAN_ATTEMPTS retries with NoOpLLM, 6 for the fail-level issue's MAX_CLEAN_ATTEMPTS
retries with AlwaysFailLLM, since scenario 3 runs clean_dataset() twice total across its
own load and the earlier scenarios don't consume any approval prompts):
    printf 'yes\\nyes\\nyes\\nyes\\nyes\\nyes\\nyes\\nyes\\nyes\\n' | uv run python -m tests.test_data_quality_status
"""

import csv
from pathlib import Path

from agents.sql_analyst import add_context, generate_sql, represent_final_answer
from models.schema import SQLAnalystState
from utils.data_cleaning import clean_dataset
from utils.load_data import (
    compute_quality_status,
    ensure_data_quality_status_table,
    get_admin_connection,
    load_csv_to_table,
    unresolved_issues_for_record,
    write_data_quality_status,
)


class FakeResponse:
    def __init__(self, content):
        self.content = content


class AlwaysFailLLM:
    """Every generated-code attempt raises — used for the fail-level fixture so the
    fail-level issues are guaranteed to survive cleaning without depending on the real
    model's behavior."""

    def invoke(self, messages):
        return FakeResponse("raise RuntimeError('deliberately never fixed, for dq test')")


class NoOpLLM:
    """Generates code that runs successfully but doesn't actually change anything —
    used for the warn-only fixture so its warn-level issue deterministically survives
    cleaning (status stays 'warn') without depending on whether the real model happens
    to fix simple whitespace noise (which it usually would, defeating the point of
    this scenario)."""

    def invoke(self, messages):
        # messages[1][1] is the human content, which always contains the real cloned
        # file path inside the prompt built by _describe_file_for_prompt/callers —
        # rather than parse it out, this just re-reads/re-writes whatever path a real
        # cleaning script for this fixture would target, using pandas' own no-op
        # round trip (read then write unchanged) so the CSV content is untouched.
        return FakeResponse(
            "import pandas as pd\n"
            "path = 'data/_test_etl/dq_warn/cleaned/dq_warn_customers.csv'\n"
            "df = pd.read_csv(path, dtype=str)\n"
            "df.to_csv(path, index=False)\n"
        )


def load_and_track(conn, folder: str, csv_name: str, llm=None) -> tuple:
    """Run the same clean -> load -> write-status sequence utils/load_data.py's main()
    does, for exactly one file, and return the resulting table name."""
    folder_path = Path(folder)
    csv_path = folder_path / csv_name

    cleaning_result = clean_dataset(folder_path, llm=llm)
    records_by_name = {rec.file_name: rec for rec in cleaning_result.cleaned_files}
    records_by_name.update({rec.file_name: rec for rec in cleaning_result.skipped_files})

    if csv_name in records_by_name:
        load_path = Path(cleaning_result.cleaned_dir) / csv_name
    else:
        load_path = csv_path

    table_name, _row_count = load_csv_to_table(conn, load_path)

    rec = records_by_name.get(csv_name)
    if rec is None:
        unresolved_issues = []
        was_cleaned = False
    else:
        unresolved_issues = unresolved_issues_for_record(rec)
        was_cleaned = True
    status, issues_found = compute_quality_status(unresolved_issues)
    write_data_quality_status(conn, table_name, status, issues_found, was_cleaned)
    return table_name, status, issues_found


def cleanup_table(conn, table_name: str) -> None:
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{table_name}" CASCADE;')
        cur.execute("DELETE FROM _data_quality_status WHERE table_name = %s", (table_name,))
    conn.commit()


def fetch_status_row(conn, table_name: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, issues_found, was_cleaned FROM _data_quality_status WHERE table_name = %s",
            (table_name,),
        )
        return cur.fetchone()


def ask(question: str) -> tuple:
    """Run curated_question=question straight through add_context -> generate_sql
    -> represent_final_answer (skipping curate_question itself and is_safe/execute_sql
    plumbing, same pattern as tests/test_generate_sql.py), and return
    (final_answer, prompt_query_context)."""
    state = SQLAnalystState(user_question=question, curated_question=question)
    ctx_result = add_context(state)
    state = state.model_copy(update=ctx_result)

    sql_result = generate_sql(state)
    state = state.model_copy(update=sql_result)

    # This test only exercises add_context/represent_final_answer's data-quality
    # wiring, not real execution — a hand-built plausible execution_result is enough
    # for represent_final_answer to produce a real natural-language answer that either
    # does or doesn't mention data quality.
    state = state.model_copy(update={"sql_query_execution_result": "[{'n': 8}]"})

    final_result = represent_final_answer(state)
    return final_result["final_answer"], state.prompt_query_context


conn = get_admin_connection()
ensure_data_quality_status_table(conn)

created_tables = []

try:
    print("=" * 70)
    print("SCENARIO 1: clean table -> status 'pass', no warning in the answer")
    print("=" * 70)
    table1, status1, issues1 = load_and_track(conn, "data/_test_etl/dq_clean", "dq_clean_products.csv")
    created_tables.append(table1)
    print(f"table={table1} status={status1} issues={issues1}")
    assert status1 == "pass", f"expected status 'pass', got {status1!r}"
    assert issues1 == [], f"expected no issues recorded, got {issues1}"
    row1 = fetch_status_row(conn, table1)
    assert row1 is not None, "expected a status row to exist"
    assert row1[0] == "pass"
    print("PASS: status row is 'pass' with no issues recorded.")

    answer1, ctx1 = ask(f"How many rows are in {table1}?")
    warning_lines1 = [line for line in ctx1.splitlines() if line.startswith("WARNING") and f"WARNING: {table1} " in line]
    print("context excerpt:", warning_lines1)
    print("final answer:", answer1)
    assert warning_lines1 == [], f"expected no WARNING line at all for the clean table, got: {warning_lines1}"
    assert "quality" not in answer1.lower() and "never been checked" not in answer1.lower(), (
        f"expected no data-quality mention in the answer for a clean table, got: {answer1}"
    )
    print("PASS: no data-quality warning surfaced for the clean table.\n")

    print("=" * 70)
    print("SCENARIO 2: warn-only table -> status 'warn', still NOT surfaced in the answer")
    print("=" * 70)
    table2, status2, issues2 = load_and_track(
        conn, "data/_test_etl/dq_warn", "dq_warn_customers.csv", llm=NoOpLLM()
    )
    created_tables.append(table2)
    print(f"table={table2} status={status2} issues={issues2}")
    assert status2 == "warn", f"expected status 'warn', got {status2!r}"
    assert len(issues2) >= 1 and all(e["severity"] == "warn" for e in issues2), (
        f"expected only warn-severity issues recorded, got {issues2}"
    )
    row2 = fetch_status_row(conn, table2)
    assert row2 is not None and row2[0] == "warn"
    print("PASS: status row is 'warn' with only warn-severity issues recorded.")

    answer2, ctx2 = ask(f"How many rows are in {table2}?")
    print("final answer:", answer2)
    warning_lines2 = [line for line in ctx2.splitlines() if line.startswith("WARNING") and f"WARNING: {table2} " in line]
    assert warning_lines2 == [], (
        f"expected NO generate_sql context WARNING for a warn-only table, got: {warning_lines2}"
    )
    assert "quality" not in answer2.lower() and "never been checked" not in answer2.lower(), (
        f"expected warn-level status to stay out of the final answer, got: {answer2}"
    )
    print("PASS: warn-level status correctly stayed out of both the context and the answer.\n")

    print("=" * 70)
    print("SCENARIO 3: fail-level table, cleaning can't resolve it -> status 'fail', warning DOES surface")
    print("=" * 70)
    table3, status3, issues3 = load_and_track(
        conn, "data/_test_etl/dq_fail", "dq_fail_orders.csv", llm=AlwaysFailLLM()
    )
    created_tables.append(table3)
    print(f"table={table3} status={status3} issues={issues3}")
    assert status3 == "fail", f"expected status 'fail', got {status3!r}"
    assert any(e["severity"] == "fail" for e in issues3), f"expected a fail-severity issue, got {issues3}"
    row3 = fetch_status_row(conn, table3)
    assert row3 is not None and row3[0] == "fail"
    print("PASS: status row is 'fail' with a real fail-severity issue recorded.")

    answer3, ctx3 = ask(f"How many rows are in {table3}?")
    print("context excerpt:", [line for line in ctx3.splitlines() if line.startswith("WARNING")])
    print("final answer:", answer3)
    warning_lines3 = [
        line
        for line in ctx3.splitlines()
        if line.startswith("WARNING") and table3 in line and "unresolved critical" in line
    ]
    assert warning_lines3, f"expected a generate_sql context WARNING for the fail-level table {table3}"
    assert "unresolved" in warning_lines3[0].lower() or "critical" in warning_lines3[0].lower()
    print("PASS: generate_sql context carries the fail-level WARNING.")
    assert any(kw in answer3.lower() for kw in ("quality", "unresolved", "issue")), (
        f"expected the final answer to mention the unresolved data-quality issue, got: {answer3}"
    )
    print("PASS: final answer surfaces a clear data-quality warning.\n")

    print("=" * 70)
    print("SCENARIO 4: no status row at all (bypassing load_data.py) -> 'never checked' warning")
    print("=" * 70)
    table4 = "dq_never_checked_table"
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{table4}" CASCADE;')
        cur.execute(f'CREATE TABLE "{table4}" (id BIGINT, note TEXT);')
        cur.execute(f'INSERT INTO "{table4}" (id, note) VALUES (%s, %s)', (1, "inserted directly, bypassing load_data.py"))
    conn.commit()
    created_tables.append(table4)

    row4 = fetch_status_row(conn, table4)
    assert row4 is None, "expected no status row for a table inserted directly"
    print("PASS: confirmed no _data_quality_status row exists for this table.")

    answer4, ctx4 = ask(f"How many rows are in {table4}?")
    print("context excerpt:", [line for line in ctx4.splitlines() if line.startswith("WARNING")])
    print("final answer:", answer4)
    warning_lines4 = [line for line in ctx4.splitlines() if line.startswith("WARNING") and f"WARNING: {table4} " in line]
    assert warning_lines4, f"expected a 'no recorded data-quality check' WARNING for {table4}"
    assert "no recorded data-quality check" in warning_lines4[0]
    print("PASS: generate_sql context carries the 'never checked' WARNING.")
    assert any(kw in answer4.lower() for kw in ("never", "no recorded", "not been checked", "quality")), (
        f"expected the final answer to mention the table was never checked, got: {answer4}"
    )
    print("PASS: final answer surfaces the 'never checked' warning.\n")

    print("=" * 70)
    print("ALL DATA-QUALITY-STATUS ASSERTIONS PASSED")
    print("=" * 70)
finally:
    for t in created_tables:
        cleanup_table(conn, t)
    conn.close()
