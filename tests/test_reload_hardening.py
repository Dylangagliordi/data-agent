"""Tests for Tier 4 hardening of the auto-cleaning/reload workflow (architecture
review points #20/#21) — the one feature in this project that can mutate real, live
data in response to a question.

Five scenarios:

1. (db) Atomic table replacement + rollback: load_csv_to_table swaps a staging table
   into place atomically; the table it replaces becomes <table>_previous instead of
   being dropped; rollback_table() swaps it back.
2. (db) Forced mid-load failure: a malformed row partway through a load must leave
   the existing real table completely untouched and queryable — no partial state.
3. (db) Source checksum freshness: check_source_freshness correctly reports no-change
   when there's no baseline or the file is byte-identical, and correctly detects a
   genuine change after the raw file is modified.
4. (clean) Versioned cleaned artifacts: running clean_dataset() twice on the same
   fixture (with the raw file mutated between runs) leaves cleaned/<file> holding the
   latest result and cleaned/<file>.previous holding the one before it.
5. (clean) clean_and_reload logs a checksum-mismatch message (not silence) when the
   real source file changes between two auto-redirect runs on the same table.

Run scenarios 1-3 (no stdin needed):
    uv run python -m tests.test_reload_hardening db

Run scenarios 4-5 (need piped 'yes' answers — one clean_dataset approval per run,
four runs total across both scenarios):
    printf 'yes\\nyes\\nyes\\nyes\\n' | uv run python -m tests.test_reload_hardening clean
"""

import csv
import io
import sys
from pathlib import Path
from unittest.mock import patch

from utils.load_data import (
    check_source_freshness,
    ensure_data_quality_status_table,
    get_admin_connection,
    load_csv_to_table,
    rollback_table,
    write_data_quality_status,
)

SCRATCH_DIR = Path("data/_test_etl/reload_hardening/_scratch")
FOLDER = "data/_test_etl/reload_hardening"
CSV_NAME = "reload_dup.csv"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _write_csv(path: Path, rows: list, header: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _count_csv_data_rows(path: Path) -> int:
    with open(path, newline="") as f:
        r = csv.reader(f)
        next(r)  # skip header
        return sum(1 for _ in r)


def _table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table_name,))
        exists = cur.fetchone()[0]
    conn.commit()
    return exists


def _row_count_of(conn, table_name: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f'SELECT COUNT(*) FROM "{table_name}"')
        count = cur.fetchone()[0]
    conn.commit()
    return count


def _drop(conn, *table_names) -> None:
    with conn.cursor() as cur:
        for t in table_names:
            cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
    conn.commit()


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeDedupLLM:
    """Generates working dedup code by parsing the target path out of the prompt.
    Same deterministic pattern used in tests/test_auto_clean_redirect.py."""

    def invoke(self, messages):
        human_content = messages[1][1]
        path = human_content.split("\n")[0].split(": ", 1)[1].strip()
        code = (
            "import pandas as pd\n"
            f"df = pd.read_csv('{path}', dtype=str)\n"
            "df = df.drop_duplicates()\n"
            f"df.to_csv('{path}', index=False)\n"
        )
        return FakeResponse(code)


# ---------------------------------------------------------------------------
# Scenario 1: atomic swap + rollback
# ---------------------------------------------------------------------------

def run_atomic_swap_and_rollback():
    print("=" * 70)
    print("SCENARIO 1: atomic table replacement + rollback_table()")
    print("=" * 70)
    table = "reload_hardening_atomic"
    conn = get_admin_connection()
    csv_path = SCRATCH_DIR / f"{table}.csv"
    try:
        ensure_data_quality_status_table(conn)
        _drop(conn, table, f"{table}_previous", f"__reload_staging__{table}")

        # v1: 2 rows
        _write_csv(csv_path, [["1", "10"], ["2", "20"]], ["id", "val"])
        table_name, row_count = load_csv_to_table(conn, csv_path)
        assert table_name == table
        assert row_count == 2
        assert _table_exists(conn, table), "table must exist after first load"
        assert not _table_exists(conn, f"{table}_previous"), "no _previous on first-ever load"
        print(f"PASS: first load created '{table}' with {row_count} rows; no _previous yet.")

        # v2: 3 rows, different content
        _write_csv(csv_path, [["1", "10"], ["2", "20"], ["3", "30"]], ["id", "val"])
        table_name2, row_count2 = load_csv_to_table(conn, csv_path)
        assert row_count2 == 3
        assert _table_exists(conn, table)
        assert _table_exists(conn, f"{table}_previous"), "second load must produce a _previous"
        assert _row_count_of(conn, table) == 3, "current table must reflect v2"
        assert _row_count_of(conn, f"{table}_previous") == 2, "_previous must reflect v1"
        print("PASS: second load atomically swapped in v2 (3 rows); v1 (2 rows) preserved as _previous.")
        print("PASS: no window where the table was missing or half-populated (swap is one transaction).")

        # rollback
        rolled_back = rollback_table(conn, table)
        assert rolled_back is True
        assert _row_count_of(conn, table) == 2, "rollback must restore v1 content"
        assert _row_count_of(conn, f"{table}_previous") == 3, "v2 becomes the new _previous"
        print("PASS: rollback_table() restored v1 content; v2 now sits as _previous.")

        # rollback again -> toggles back to v2 (real swap, not destructive overwrite)
        rolled_back2 = rollback_table(conn, table)
        assert rolled_back2 is True
        assert _row_count_of(conn, table) == 3
        print("PASS: a second rollback_table() call toggles back to v2 — real swap semantics.")

        print("\nSCENARIO 1 PASSED\n")
    finally:
        _drop(conn, table, f"{table}_previous", f"__reload_staging__{table}")
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 2: forced mid-load failure -> existing table remains intact
# ---------------------------------------------------------------------------

def run_forced_failure():
    print("=" * 70)
    print("SCENARIO 2: forced mid-load failure leaves the existing table fully intact")
    print("=" * 70)
    table = "reload_hardening_failtest"
    conn = get_admin_connection()
    csv_path = SCRATCH_DIR / f"{table}.csv"
    try:
        ensure_data_quality_status_table(conn)
        _drop(conn, table, f"{table}_previous", f"__reload_staging__{table}")

        _write_csv(csv_path, [["1", "10.0"], ["2", "20.0"], ["3", "30.0"]], ["id", "val"])
        table_name, row_count = load_csv_to_table(conn, csv_path)
        assert row_count == 3
        print(f"Baseline: '{table}' loaded with {row_count} rows.")

        # Overwrite the same source path with a malformed row. sample_rows_for_typing=0
        # samples only the FIRST data row (id='1' -> BIGINT inferred), so the later
        # non-numeric id fails the INSERT partway through the load.
        _write_csv(
            csv_path,
            [["1", "10.0"], ["2", "20.0"], ["notanumber", "40.0"], ["4", "50.0"]],
            ["id", "val"],
        )

        raised = False
        try:
            load_csv_to_table(conn, csv_path, sample_rows_for_typing=0)
        except Exception as e:
            raised = True
            print(f"Load correctly raised: {type(e).__name__}: {e}")
        assert raised, "expected load_csv_to_table to raise on a malformed row"

        assert _table_exists(conn, table), "table must still exist after a failed reload"
        assert _row_count_of(conn, table) == 3, "table content must be completely unchanged"
        assert not _table_exists(conn, f"{table}_previous"), "no swap happened -> no _previous"
        assert not _table_exists(conn, f"__reload_staging__{table}"), "failed staging table must not linger"
        print("PASS: existing table remains fully intact and queryable — no partial/broken state.")
        print("PASS: no orphan staging or _previous table left behind by the failed load.")

        print("\nSCENARIO 2 PASSED\n")
    finally:
        _drop(conn, table, f"{table}_previous", f"__reload_staging__{table}")
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 3: source checksum freshness detection
# ---------------------------------------------------------------------------

def run_checksum_freshness():
    print("=" * 70)
    print("SCENARIO 3: source checksum freshness detection")
    print("=" * 70)
    table = "reload_hardening_checksum"
    conn = get_admin_connection()
    csv_path = SCRATCH_DIR / f"{table}.csv"
    try:
        ensure_data_quality_status_table(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM _data_quality_status WHERE table_name = %s", (table,))
        conn.commit()

        _write_csv(csv_path, [["1", "10"]], ["id", "val"])

        changed0, checksum0 = check_source_freshness(conn, table, csv_path)
        assert changed0 is False, "no stored baseline yet -> must not report 'changed'"
        print(f"PASS: no baseline recorded -> source_changed=False (checksum={checksum0[:12]}...).")

        write_data_quality_status(
            conn, table, "pass", [], was_cleaned=False, source_checksum=checksum0,
        )

        changed1, checksum1 = check_source_freshness(conn, table, csv_path)
        assert changed1 is False, "unchanged file must not report 'changed'"
        assert checksum1 == checksum0
        print("PASS: unchanged file -> source_changed=False, checksum matches stored baseline.")

        _write_csv(csv_path, [["1", "10"], ["2", "999"]], ["id", "val"])
        changed2, checksum2 = check_source_freshness(conn, table, csv_path)
        assert changed2 is True, "a genuinely modified source file must be detected as changed"
        assert checksum2 != checksum0
        print("PASS: modified file -> source_changed=True, checksum differs from the stored baseline.")

        print("\nSCENARIO 3 PASSED\n")
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM _data_quality_status WHERE table_name = %s", (table,))
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 4: cleaned/<file> + cleaned/<file>.previous versioning
# ---------------------------------------------------------------------------

def run_cleaned_versioning():
    print("=" * 70)
    print("SCENARIO 4: cleaned/<file> + cleaned/<file>.previous versioning")
    print("(pipe 'yes' to stdin once per clean_dataset() call — 2 needed here)")
    print("=" * 70)
    from utils.data_cleaning import clean_dataset

    folder = Path(FOLDER)
    raw_path = folder / CSV_NAME
    cleaned_path = folder / "cleaned" / CSV_NAME
    previous_path = folder / "cleaned" / f"{CSV_NAME}.previous"

    _write_csv(raw_path, [["a", "10.5"], ["b", "20.0"], ["b", "20.0"], ["c", "15.5"]], ["label", "amount"])
    if cleaned_path.exists():
        cleaned_path.unlink()
    if previous_path.exists():
        previous_path.unlink()

    result1 = clean_dataset(folder, llm=FakeDedupLLM(), trigger="manual")
    assert any(r.file_name == CSV_NAME for r in result1.cleaned_files), "run 1 should clean the file"
    assert cleaned_path.exists(), "cleaned/<file> must exist after run 1"
    assert not previous_path.exists(), "no .previous should exist after the very first run"
    row_count_1 = _count_csv_data_rows(cleaned_path)
    print(f"Run 1: cleaned/{CSV_NAME} has {row_count_1} data rows; no .previous yet.")

    # Mutate the RAW source before run 2: adds a second, distinct duplicate pair, so
    # run 2's cleaned result is genuinely different from run 1's.
    _write_csv(
        raw_path,
        [["a", "10.5"], ["b", "20.0"], ["b", "20.0"], ["c", "15.5"], ["d", "30.0"], ["d", "30.0"]],
        ["label", "amount"],
    )

    result2 = clean_dataset(folder, llm=FakeDedupLLM(), trigger="manual")
    assert any(r.file_name == CSV_NAME for r in result2.cleaned_files), "run 2 should clean the file"
    assert cleaned_path.exists()
    assert previous_path.exists(), "cleaned/<file>.previous must exist after the second run"
    row_count_2 = _count_csv_data_rows(cleaned_path)
    row_count_previous = _count_csv_data_rows(previous_path)

    print(f"Run 2: cleaned/{CSV_NAME} now has {row_count_2} data rows.")
    print(f"       cleaned/{CSV_NAME}.previous has {row_count_previous} data rows (run 1's result).")

    assert row_count_previous == row_count_1, ".previous must reflect run 1's cleaned result"
    assert row_count_2 != row_count_1, "current clone must reflect run 2's genuinely different result"
    print("PASS: cleaned/<file> and cleaned/<file>.previous correctly reflect current vs. prior versions.")

    print("\nSCENARIO 4 PASSED\n")


# ---------------------------------------------------------------------------
# Scenario 5: clean_and_reload logs a checksum mismatch when the source changes
# ---------------------------------------------------------------------------

def run_checksum_integration_logging():
    print("=" * 70)
    print("SCENARIO 5: clean_and_reload logs a checksum mismatch on a real source change")
    print("(pipe 'yes' to stdin once per clean_dataset() call — 2 needed here)")
    print("=" * 70)
    from agents.sql_analyst import clean_and_reload
    from models.schema import SQLAnalystState

    folder = Path(FOLDER)
    raw_path = folder / CSV_NAME
    table = "reload_dup"

    conn = get_admin_connection()
    try:
        ensure_data_quality_status_table(conn)
        _drop(conn, table, f"{table}_previous", f"__reload_staging__{table}")

        _write_csv(raw_path, [["a", "10.5"], ["b", "20.0"], ["b", "20.0"], ["c", "15.5"]], ["label", "amount"])
        write_data_quality_status(
            conn, table, "fail",
            [{"issue": "Duplicate rows: 1 fully duplicate rows found.", "severity": "fail"}],
            was_cleaned=False, source_folder=str(folder),
        )

        state = SQLAnalystState(
            user_question=f"How many rows are in {table}?",
            curated_question=f"How many rows are in {table}?",
            tables_to_clean=[{"table": table, "source_folder": str(folder)}],
            cleaning_attempted_tables=[],
        )

        # First run establishes a checksum baseline — no mismatch expected. Piping
        # scripted 'yes' answers to a non-tty stdin simulates an interactive
        # approving user, so tell clean_and_reload's non-interactive fail-closed
        # gate (architecture review point #24) that stdin is interactive here.
        old_stderr = sys.stderr
        buf1 = io.StringIO()
        sys.stderr = buf1
        try:
            with patch("sys.stdin.isatty", return_value=True):
                reload_result_1 = clean_and_reload(state, _llm=FakeDedupLLM())
        finally:
            sys.stderr = old_stderr
        first_log = buf1.getvalue()
        assert "[checksum]" not in first_log, (
            f"first-ever run has no baseline to compare against -> expected no mismatch log, got: {first_log!r}"
        )
        print("PASS: first-ever clean_and_reload run logs no checksum mismatch (no baseline yet).")

        # Mutate the raw source before the second run.
        _write_csv(
            raw_path,
            [["a", "10.5"], ["b", "20.0"], ["b", "20.0"], ["c", "15.5"], ["d", "30.0"], ["d", "30.0"]],
            ["label", "amount"],
        )

        state2 = state.model_copy(update=reload_result_1)
        buf2 = io.StringIO()
        sys.stderr = buf2
        try:
            with patch("sys.stdin.isatty", return_value=True):
                clean_and_reload(state2, _llm=FakeDedupLLM())
        finally:
            sys.stderr = old_stderr
        second_log = buf2.getvalue()
        print(f"Second run stderr: {second_log!r}")
        assert "[checksum]" in second_log and "forcing fresh clean" in second_log, (
            f"expected a checksum-mismatch log line on the second run, got: {second_log!r}"
        )
        print("PASS: modified source file correctly detected and logged before the fresh clean ran.")

        print("\nSCENARIO 5 PASSED\n")
    finally:
        _drop(conn, table, f"{table}_previous", f"__reload_staging__{table}")
        with conn.cursor() as cur:
            cur.execute("DELETE FROM _data_quality_status WHERE table_name = %s", (table,))
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def usage():
    print(
        "Usage:\n"
        "  uv run python -m tests.test_reload_hardening db\n"
        "  printf 'yes\\nyes\\nyes\\nyes\\n' | uv run python -m tests.test_reload_hardening clean\n"
    )


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "db"

    if mode == "db":
        run_atomic_swap_and_rollback()
        run_forced_failure()
        run_checksum_freshness()
        print("=" * 70)
        print("ALL DB-ONLY SCENARIOS (1-3) PASSED")
        print("=" * 70)
    elif mode == "clean":
        run_cleaned_versioning()
        run_checksum_integration_logging()
        print("=" * 70)
        print("ALL CLEANING SCENARIOS (4-5) PASSED")
        print("=" * 70)
    else:
        usage()
        sys.exit(1)
