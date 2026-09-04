"""Tests for the auto-clean redirect: when a question touches a table with an
unresolved fail-level data-quality issue and a known source_folder, the SQL analyst
graph automatically cleans and reloads it before answering.

Five scenarios, all running against real Postgres via admin + app_reader connections:

1. Fail table with valid source_folder, user approves cleaning:
   - add_context sets data_quality_action="needs_cleaning"
   - clean_and_reload fires, cleaning succeeds, table reloads, status -> "pass"
   - Second add_context: data_quality_action="proceed", no warning in data_quality_warnings
   REQUIRES: printf 'yes\\nyes\\n' piped to stdin (one "yes" per fail-level issue)

2. Fail table with valid source_folder, user declines cleaning:
   - clean_and_reload fires, user declines, status stays "fail"
   - cleaning_attempted_tables populated -> redirect does NOT fire again
   - Second add_context: data_quality_action="proceed", warning still present
   REQUIRES: printf 'no\\n' piped to stdin

3. Fail table with source_folder=null:
   - add_context sees fail status but no source_folder -> tables_to_clean empty
   - data_quality_action="proceed", no redirect, warning injected (existing behavior)

4. Pass/warn table:
   - add_context: no fail status -> tables_to_clean empty, data_quality_action="proceed"
   - Existing behavior completely unchanged

5. (Instructions) Re-run the full existing test suite for regressions.

Run scenarios 1 and 2 separately with the appropriate piped stdin:

  Scenario 1 (approve):
    printf 'yes\\nyes\\n' | uv run python -m tests.test_auto_clean_redirect approve

  Scenario 2 (decline):
    printf 'no\\n' | uv run python -m tests.test_auto_clean_redirect decline

  Scenarios 3+4 (no stdin needed):
    uv run python -m tests.test_auto_clean_redirect routing

  All three modes (approve needs piped input; run it last to not consume stdin):
    uv run python -m tests.test_auto_clean_redirect routing
    printf 'no\\n' | uv run python -m tests.test_auto_clean_redirect decline
    printf 'yes\\nyes\\n' | uv run python -m tests.test_auto_clean_redirect approve
"""

import sys
from pathlib import Path

from agents.sql_analyst import add_context, clean_and_reload
from models.schema import SQLAnalystState
from utils.load_data import (
    compute_quality_status,
    ensure_data_quality_status_table,
    get_admin_connection,
    load_csv_to_table,
    write_data_quality_status,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

FAIL_FIXTURE_FOLDER = "data/_test_etl/dq_fail"
FAIL_CSV_NAME = "dq_fail_orders.csv"
FAIL_TABLE = "dq_fail_orders"

CLEAN_FIXTURE_FOLDER = "data/_test_etl/dq_clean"
CLEAN_CSV_NAME = "dq_clean_products.csv"
CLEAN_TABLE = "dq_clean_products"


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeDedupLLM:
    """Generates working dedup code by parsing the target path out of the prompt.
    Removes duplicate rows from whatever CSV file clean_dataset is processing.
    Used for scenario 1 (redirect succeeds) to keep the cleaning step deterministic.
    """

    def invoke(self, messages):
        # _generate_cleaning_code's human prompt starts with:
        # "File to clean (read and overwrite this exact path): <path>"
        human_content = messages[1][1]
        path = human_content.split("\n")[0].split(": ", 1)[1].strip()
        code = (
            "import pandas as pd\n"
            f"df = pd.read_csv('{path}', dtype=str)\n"
            "df = df.drop_duplicates()\n"
            f"df.to_csv('{path}', index=False)\n"
        )
        return FakeResponse(code)


class AnyCodeLLM:
    """Generates trivial code that the user can decline. Used for scenario 2
    (user declines) — the code never executes, so its content doesn't matter."""

    def invoke(self, messages):
        human_content = messages[1][1]
        path = human_content.split("\n")[0].split(": ", 1)[1].strip()
        return FakeResponse(f"# placeholder\nimport pandas as pd\ndf = pd.read_csv('{path}', dtype=str)\n")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _setup_fail_table_with_source(conn, source_folder: str | None) -> None:
    """Load the raw dq_fail CSV directly (no cleaning) and write a fail-level
    status row with the given source_folder (may be None for scenario 3)."""
    load_csv_to_table(conn, Path(FAIL_FIXTURE_FOLDER) / FAIL_CSV_NAME)
    issues_found = [
        {"issue": "Duplicate rows: 1 fully duplicate rows found.", "severity": "fail"},
        {"issue": f"Duplicate values: column 'order_id' looks like a unique identifier but has 1 duplicate value.", "severity": "fail"},
    ]
    write_data_quality_status(
        conn, FAIL_TABLE, "fail", issues_found, was_cleaned=False,
        source_folder=source_folder,
    )


def _setup_pass_table(conn) -> None:
    """Load the clean fixture and write a pass status row."""
    load_csv_to_table(conn, Path(CLEAN_FIXTURE_FOLDER) / CLEAN_CSV_NAME)
    write_data_quality_status(
        conn, CLEAN_TABLE, "pass", [], was_cleaned=False, source_folder=CLEAN_FIXTURE_FOLDER,
    )


def _cleanup(conn, *table_names) -> None:
    with conn.cursor() as cur:
        for t in table_names:
            cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
            # load_csv_to_table's atomic swap (Tier 4 hardening) can leave a
            # <table>_previous behind — drop it too so repeated runs don't accumulate it.
            cur.execute(f'DROP TABLE IF EXISTS "{t}_previous" CASCADE')
            cur.execute("DELETE FROM _data_quality_status WHERE table_name = %s", (t,))
    conn.commit()


def _fetch_status(conn, table_name: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, source_folder FROM _data_quality_status WHERE table_name = %s",
            (table_name,),
        )
        row = cur.fetchone()
    # Commit immediately so this connection releases its SHARE lock on
    # _data_quality_status before clean_and_reload opens a second admin connection
    # and tries ALTER TABLE (which needs ACCESS EXCLUSIVE — blocked by a live
    # SHARE lock from an uncommitted SELECT on the same connection).
    conn.commit()
    return row


# ---------------------------------------------------------------------------
# Scenario 3+4: pure routing, no LLM, no approval
# ---------------------------------------------------------------------------

def run_routing_scenarios():
    print("=" * 70)
    print("SCENARIO 3: fail table with source_folder=null -> no redirect fires")
    print("=" * 70)
    conn = get_admin_connection()
    try:
        ensure_data_quality_status_table(conn)
        _setup_fail_table_with_source(conn, source_folder=None)

        state = SQLAnalystState(
            user_question=f"How many rows are in {FAIL_TABLE}?",
            curated_question=f"How many rows are in {FAIL_TABLE}?",
        )
        result = add_context(state)
        action = result["data_quality_action"]
        to_clean = result["tables_to_clean"]
        warnings = result["data_quality_warnings"]

        print(f"data_quality_action: {action}")
        print(f"tables_to_clean: {to_clean}")
        print(f"data_quality_warnings: {[w['warning'] for w in warnings]}")

        assert action == "proceed", f"expected 'proceed' with null source_folder, got {action!r}"
        assert to_clean == [], f"expected empty tables_to_clean, got {to_clean}"
        # The fail warning still surfaces into generate_sql's context (warning-only
        # behavior, exactly as before this feature)
        fail_warnings = [w for w in warnings if FAIL_TABLE in w["table"] and "unresolved critical" in w["warning"]]
        assert fail_warnings, "expected the fail-level warning to still be present in data_quality_warnings"
        print("PASS: null source_folder -> routing is 'proceed', warning still injected.\n")
    finally:
        _cleanup(conn, FAIL_TABLE)
        conn.close()

    print("=" * 70)
    print("SCENARIO 4: pass table -> add_context unchanged, no redirect at all")
    print("=" * 70)
    conn = get_admin_connection()
    try:
        ensure_data_quality_status_table(conn)
        _setup_pass_table(conn)

        state = SQLAnalystState(
            user_question=f"How many rows are in {CLEAN_TABLE}?",
            curated_question=f"How many rows are in {CLEAN_TABLE}?",
        )
        result = add_context(state)
        action = result["data_quality_action"]
        to_clean = result["tables_to_clean"]
        warnings = result["data_quality_warnings"]

        print(f"data_quality_action: {action}")
        print(f"tables_to_clean: {to_clean}")
        warn_for_clean = [w for w in warnings if CLEAN_TABLE in w["table"]]
        print(f"warnings for {CLEAN_TABLE}: {warn_for_clean}")

        assert action == "proceed", f"expected 'proceed' for pass table, got {action!r}"
        assert to_clean == [], f"expected empty tables_to_clean, got {to_clean}"
        assert warn_for_clean == [], f"pass-status table should not appear in data_quality_warnings, got {warn_for_clean}"
        print("PASS: pass table -> 'proceed', no warning, no redirect.\n")
    finally:
        _cleanup(conn, CLEAN_TABLE)
        conn.close()

    print("=" * 70)
    print("SCENARIOS 3+4 PASSED")
    print("=" * 70)


# ---------------------------------------------------------------------------
# Scenario 1: redirect fires, user approves, table cleans to "pass"
# ---------------------------------------------------------------------------

def run_approve_scenario():
    print("=" * 70)
    print("SCENARIO 1: fail table with valid source_folder, user approves")
    print("(pipe 'yes\\nyes\\n' to stdin before running this)")
    print("=" * 70)
    conn = get_admin_connection()
    try:
        ensure_data_quality_status_table(conn)
        _setup_fail_table_with_source(conn, source_folder=FAIL_FIXTURE_FOLDER)

        # Verify initial DB state
        row = _fetch_status(conn, FAIL_TABLE)
        assert row is not None and row[0] == "fail", f"expected fail status initially, got {row}"
        assert row[1] == FAIL_FIXTURE_FOLDER, f"expected source_folder set, got {row[1]}"
        print(f"Initial state: status={row[0]}, source_folder={row[1]}")

        # --- First add_context pass ---
        state = SQLAnalystState(
            user_question=f"How many rows are in {FAIL_TABLE}?",
            curated_question=f"How many rows are in {FAIL_TABLE}?",
            cleaning_attempted_tables=[],
        )
        ctx1 = add_context(state)
        state = state.model_copy(update=ctx1)

        print(f"First add_context -> data_quality_action: {state.data_quality_action}")
        print(f"tables_to_clean: {state.tables_to_clean}")
        assert state.data_quality_action == "needs_cleaning", (
            f"expected 'needs_cleaning', got {state.data_quality_action!r}"
        )
        assert any(item["table"] == FAIL_TABLE for item in state.tables_to_clean), (
            f"expected {FAIL_TABLE} in tables_to_clean, got {state.tables_to_clean}"
        )
        print("PASS: first add_context correctly sets data_quality_action='needs_cleaning'.")

        # --- clean_and_reload (FakeDedupLLM + piped 'yes yes' via stdin) ---
        print(f"\nRunning clean_and_reload (approve the prompts with 'yes')...")
        reload_result = clean_and_reload(state, _llm=FakeDedupLLM())
        state = state.model_copy(update=reload_result)

        print(f"cleaning_attempted_tables after: {state.cleaning_attempted_tables}")
        assert FAIL_TABLE in state.cleaning_attempted_tables, (
            f"expected {FAIL_TABLE} in cleaning_attempted_tables after clean_and_reload"
        )

        # Verify DB status was updated
        row_after = _fetch_status(conn, FAIL_TABLE)
        print(f"DB status after clean_and_reload: status={row_after[0]}, source_folder={row_after[1]}")
        assert row_after[0] in ("pass", "warn"), (
            f"expected status to improve to 'pass' or 'warn' after successful cleaning, got {row_after[0]!r}"
        )
        assert row_after[1] == FAIL_FIXTURE_FOLDER, "source_folder must be preserved after reload"
        print(f"PASS: table reloaded with status={row_after[0]!r}.")

        # --- Second add_context pass (after successful clean+reload) ---
        ctx2 = add_context(state)
        state = state.model_copy(update=ctx2)

        print(f"\nSecond add_context -> data_quality_action: {state.data_quality_action}")
        fail_warnings = [
            w for w in state.data_quality_warnings
            if FAIL_TABLE in w["table"] and "unresolved critical" in w["warning"]
        ]
        print(f"Fail warnings for {FAIL_TABLE}: {fail_warnings}")
        assert state.data_quality_action == "proceed", (
            f"expected 'proceed' after successful cleaning, got {state.data_quality_action!r}"
        )
        assert fail_warnings == [], (
            f"expected no fail warning after cleaning resolved the issues, got {fail_warnings}"
        )
        print("PASS: second add_context is 'proceed' with no fail warning — question would be answered normally.")

        print("\n" + "=" * 70)
        print("SCENARIO 1 PASSED")
        print("=" * 70)
    finally:
        _cleanup(conn, FAIL_TABLE)
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 2: redirect fires, user declines, no loop
# ---------------------------------------------------------------------------

def run_decline_scenario():
    print("=" * 70)
    print("SCENARIO 2: fail table with valid source_folder, user DECLINES cleaning")
    print("(pipe 'no\\n' to stdin before running this)")
    print("=" * 70)
    conn = get_admin_connection()
    try:
        ensure_data_quality_status_table(conn)
        _setup_fail_table_with_source(conn, source_folder=FAIL_FIXTURE_FOLDER)

        # --- First add_context pass ---
        state = SQLAnalystState(
            user_question=f"How many rows are in {FAIL_TABLE}?",
            curated_question=f"How many rows are in {FAIL_TABLE}?",
            cleaning_attempted_tables=[],
        )
        ctx1 = add_context(state)
        state = state.model_copy(update=ctx1)

        print(f"First add_context -> data_quality_action: {state.data_quality_action}")
        assert state.data_quality_action == "needs_cleaning", (
            f"expected 'needs_cleaning', got {state.data_quality_action!r}"
        )
        print("PASS: first add_context correctly sets data_quality_action='needs_cleaning'.")

        # --- clean_and_reload (AnyCodeLLM + piped 'no' via stdin to decline) ---
        print(f"\nRunning clean_and_reload (decline the prompt by typing 'no')...")
        reload_result = clean_and_reload(state, _llm=AnyCodeLLM())
        state = state.model_copy(update=reload_result)

        print(f"cleaning_attempted_tables after: {state.cleaning_attempted_tables}")
        assert FAIL_TABLE in state.cleaning_attempted_tables, (
            f"expected {FAIL_TABLE} in cleaning_attempted_tables even after decline"
        )

        # DB status must still be "fail" (cleaning was declined)
        row_after = _fetch_status(conn, FAIL_TABLE)
        print(f"DB status after declined clean_and_reload: {row_after[0]}")
        assert row_after[0] == "fail", (
            f"expected status to remain 'fail' after decline, got {row_after[0]!r}"
        )
        print("PASS: status still 'fail' — cleaning was correctly declined.")

        # --- Second add_context pass (table still fail, but already attempted) ---
        ctx2 = add_context(state)
        state = state.model_copy(update=ctx2)

        print(f"\nSecond add_context -> data_quality_action: {state.data_quality_action}")
        fail_warnings = [
            w for w in state.data_quality_warnings
            if FAIL_TABLE in w["table"] and "unresolved critical" in w["warning"]
        ]
        print(f"Fail warnings still present: {[w['warning'] for w in fail_warnings]}")
        assert state.data_quality_action == "proceed", (
            f"expected 'proceed' (stop-once rule: already attempted), got {state.data_quality_action!r}"
        )
        assert state.tables_to_clean == [], (
            f"expected empty tables_to_clean (stop-once), got {state.tables_to_clean}"
        )
        assert fail_warnings, (
            f"expected fail warning to still be present in data_quality_warnings after decline"
        )
        print("PASS: stop-once rule holds — redirect does NOT fire again.")
        print("PASS: fail warning still present — 'unresolved' behavior exactly as before this feature.\n")

        print("=" * 70)
        print("SCENARIO 2 PASSED")
        print("=" * 70)
    finally:
        _cleanup(conn, FAIL_TABLE)
        conn.close()


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def usage():
    print(
        "Usage:\n"
        "  uv run python -m tests.test_auto_clean_redirect routing\n"
        "  printf 'no\\n'       | uv run python -m tests.test_auto_clean_redirect decline\n"
        "  printf 'yes\\nyes\\n' | uv run python -m tests.test_auto_clean_redirect approve\n"
    )


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "routing"

    if mode == "routing":
        run_routing_scenarios()
    elif mode == "approve":
        run_approve_scenario()
    elif mode == "decline":
        run_decline_scenario()
    else:
        usage()
        sys.exit(1)
