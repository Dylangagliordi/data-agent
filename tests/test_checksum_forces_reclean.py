"""Test for the checksum-freshness gate in add_context (architecture review point
#22): a "pass"/"warn" _data_quality_status row was previously treated as
permanently authoritative — nothing ever re-verified it against the live source
file. check_source_freshness() existed and was called from clean_and_reload/
load_data.py's main(), but ONLY for tables already flagged "fail" (which get
reloaded regardless of the checksum result) — so a source file that changed
underneath an already-"pass"/"warn" table was silently never caught, and that
stale status kept being reused as if the file had never changed.

Fix: add_context now checks source freshness for EVERY table with a known
source_folder, not just fail-status ones. A genuine checksum mismatch forces the
table through clean_and_reload exactly like a "fail" table would, with a clear
"source changed, forcing fresh clean" warning — instead of the stale "pass"/"warn"
row being silently reused forever.

Two scenarios, against real Postgres (same app_reader/admin connections as the
rest of this project):

1. Unchanged source: a "pass"-status table whose source file has NOT changed on
   disk since it was last processed -> add_context still says "proceed", no
   forced-reclean warning. (No regression: an unchanged pass/warn table behaves
   exactly as before this fix.)
2. Changed source: the same table's source file is then modified on disk (still a
   "pass" status in the DB — nothing has re-processed it yet) -> add_context must
   now report data_quality_action="needs_cleaning", add the table to
   tables_to_clean, and inject a "forcing fresh clean" warning — even though its
   recorded status is "pass", not "fail".

No LLM or stdin is needed — this only exercises add_context's routing decision,
not clean_and_reload/clean_dataset itself.

Run with:
    uv run python -m tests.test_checksum_forces_reclean
"""

from pathlib import Path

from agents.sql_analyst import add_context
from models.schema import SQLAnalystState
from utils.load_data import (
    compute_file_checksum,
    ensure_data_quality_status_table,
    ensure_fanout_status_table,
    get_admin_connection,
    load_csv_to_table,
    sanitize_identifier,
    write_data_quality_status,
)

FOLDER = Path("data/_test_etl/checksum_gate")
CSV_NAME = "checksum_gate_table.csv"
CSV_PATH = FOLDER / CSV_NAME
TABLE = sanitize_identifier(CSV_PATH.stem)


def _write_csv(path: Path, rows: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        f.write("id,value\n")
        for row in rows:
            f.write(",".join(row) + "\n")


def _cleanup(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{TABLE}" CASCADE')
        cur.execute(f'DROP TABLE IF EXISTS "{TABLE}_previous" CASCADE')
        cur.execute("DELETE FROM _data_quality_status WHERE table_name = %s", (TABLE,))
        cur.execute("DELETE FROM _fanout_status WHERE table_name = %s", (TABLE,))
    conn.commit()


def _warnings_for(state, phrase: str) -> list:
    return [
        w["warning"] for w in state.data_quality_warnings
        if TABLE in w["table"] and phrase in w["warning"]
    ]


if __name__ == "__main__":
    print("=" * 70)
    print("TEST: checksum-drift on a 'pass'-status table forces a fresh clean")
    print("=" * 70)

    conn = get_admin_connection()
    try:
        ensure_data_quality_status_table(conn)
        ensure_fanout_status_table(conn)
        _cleanup(conn)

        _write_csv(CSV_PATH, [["1", "10"], ["2", "20"], ["3", "30"]])
        load_csv_to_table(conn, CSV_PATH)
        baseline_checksum = compute_file_checksum(CSV_PATH)
        write_data_quality_status(
            conn, TABLE, "pass", [], was_cleaned=False,
            source_folder=str(FOLDER), source_checksum=baseline_checksum,
        )
        print(f"Baseline: '{TABLE}' loaded, status='pass', checksum recorded.\n")

        # ── Scenario 1: unchanged source -> no regression ───────────────────────
        print("SCENARIO 1: source unchanged -> add_context still says 'proceed'")
        state = SQLAnalystState(
            user_question=f"How many rows are in {TABLE}?",
            curated_question=f"How many rows are in {TABLE}?",
        )
        ctx1 = add_context(state)
        state1 = state.model_copy(update=ctx1)
        print(f"  data_quality_action: {state1.data_quality_action}")
        print(f"  tables_to_clean: {state1.tables_to_clean}")
        assert state1.data_quality_action == "proceed", (
            f"expected 'proceed' for an unchanged pass-status table, got {state1.data_quality_action!r}"
        )
        assert not any(item["table"] == TABLE for item in state1.tables_to_clean), (
            f"expected {TABLE} NOT in tables_to_clean when source is unchanged, got {state1.tables_to_clean}"
        )
        assert _warnings_for(state1, "forcing fresh clean") == [], (
            "expected no forced-reclean warning when the source file hasn't changed"
        )
        print("PASS: unchanged source -> no forced reclean, no regression.\n")

        # ── Scenario 2: source changes on disk, status row still says 'pass' ────
        print("SCENARIO 2: source file changes on disk -> forced fresh clean, even though status='pass'")
        _write_csv(CSV_PATH, [["1", "10"], ["2", "20"], ["3", "30"], ["4", "40"], ["4", "40"]])
        new_checksum = compute_file_checksum(CSV_PATH)
        assert new_checksum != baseline_checksum, "test setup error: checksum should differ after edit"

        state2_input = SQLAnalystState(
            user_question=f"How many rows are in {TABLE}?",
            curated_question=f"How many rows are in {TABLE}?",
        )
        ctx2 = add_context(state2_input)
        state2 = state2_input.model_copy(update=ctx2)
        print(f"  data_quality_action: {state2.data_quality_action}")
        print(f"  tables_to_clean: {state2.tables_to_clean}")
        print(f"  data_quality_warnings: {state2.data_quality_warnings}")

        assert state2.data_quality_action == "needs_cleaning", (
            f"expected checksum drift to force 'needs_cleaning' for a 'pass'-status "
            f"table, got {state2.data_quality_action!r} — a stale status row is "
            f"being silently reused instead of re-verified against the live source"
        )
        assert any(item["table"] == TABLE for item in state2.tables_to_clean), (
            f"expected {TABLE} in tables_to_clean after its source changed, got {state2.tables_to_clean}"
        )
        forced_warnings = _warnings_for(state2, "forcing fresh clean")
        assert forced_warnings, (
            f"expected a 'forcing fresh clean' warning for {TABLE}, got: {state2.data_quality_warnings}"
        )
        print("  Warning:", forced_warnings[0])
        print("PASS: checksum drift on a 'pass'-status table correctly forces a fresh clean.\n")

        print("=" * 70)
        print("ALL CHECKSUM-GATE ASSERTIONS PASSED")
        print("=" * 70)
    finally:
        _cleanup(conn)
        conn.close()
