"""Standalone test: main.py end to end, both question types, verifying:
1. stdout is clean (exactly the final answer, no raw state, no tracebacks).
2. logs/query_log.jsonl gets a new, correctly-shaped entry for each — an
   ETL-routed entry does NOT contain SQL-specific fields (checked directly by
   key absence, not just "it didn't crash").

Runs main.py as a real subprocess (not by importing it) so this test exercises
the exact same code path a user invoking `python main.py "..."` would hit.
"""

import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "logs" / "query_log.jsonl"

SQL_SPECIFIC_FIELDS = {
    "curated_question",
    "generated_sql_query",
    "is_safe",
    "comments",
    "sql_query_execution_result",
}


def run_main(question: str) -> str:
    result = subprocess.run(
        [sys.executable, "main.py", question],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"main.py exited non-zero ({result.returncode}) for question {question!r}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout


def last_log_entry() -> dict:
    with open(LOG_PATH) as f:
        lines = [l for l in f.read().splitlines() if l.strip()]
    return json.loads(lines[-1])


print("=" * 70)
print("CASE 1: main.py with a real SQL-shaped question")
print("=" * 70)
sql_question = "How many products are in the database in total?"
sql_stdout = run_main(sql_question)
print("stdout:", sql_stdout.strip())

assert sql_stdout.count("\n") <= 1, f"expected exactly one printed line, got: {sql_stdout!r}"
assert "route_response" not in sql_stdout and "final_answer" not in sql_stdout, (
    "expected clean stdout (just the answer), not a raw dict/state dump"
)
print("PASS: clean stdout (single line, no raw state dump).")

sql_entry = last_log_entry()
print("log entry keys:", sorted(sql_entry.keys()))
assert sql_entry["route_response"] == "sql_analyst"
assert sql_entry["user_question"] == sql_question
for field in SQL_SPECIFIC_FIELDS:
    assert field in sql_entry, f"expected SQL-specific field {field!r} present in a sql_analyst log entry"
assert sql_entry["final_answer"].strip() == sql_stdout.strip()
print("PASS: sql_analyst log entry has the full expected field set and matches stdout.\n")


print("=" * 70)
print("CASE 2: main.py with a real ETL-shaped request")
print("=" * 70)
import shutil

OUTPUT_FOLDER = "data/_test_etl/main_e2e_test"
shutil.rmtree(PROJECT_ROOT / OUTPUT_FOLDER, ignore_errors=True)
etl_question = (
    "Download this file: https://raw.githubusercontent.com/pandas-dev/pandas/main/README.md "
    f"into the folder {OUTPUT_FOLDER} as md"
)
etl_stdout = run_main(etl_question)
print("stdout:", etl_stdout.strip())

assert etl_stdout.count("\n") <= 1, f"expected exactly one printed line, got: {etl_stdout!r}"
print("PASS: clean stdout (single line, no raw state dump).")

etl_entry = last_log_entry()
print("log entry keys:", sorted(etl_entry.keys()))
assert etl_entry["route_response"] == "etl_analyst"
assert etl_entry["user_question"] == etl_question
assert etl_entry["final_answer"].strip() == etl_stdout.strip()
for field in SQL_SPECIFIC_FIELDS:
    assert field not in etl_entry, (
        f"expected NO SQL-specific field {field!r} in an etl_analyst log entry, "
        f"but found it: {etl_entry}"
    )
print("PASS: etl_analyst log entry omits every SQL-specific field entirely (not null/empty).\n")

downloaded = list((PROJECT_ROOT / OUTPUT_FOLDER).glob("*"))
assert downloaded, f"expected a real downloaded file in {OUTPUT_FOLDER}"
print(f"PASS: real file actually downloaded via main.py: {downloaded}\n")

print("=" * 70)
print("ALL main.py END-TO-END + LOG-SHAPE ASSERTIONS PASSED")
print("=" * 70)
