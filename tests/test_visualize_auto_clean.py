"""Test: auto-clean redirect fires for a visualize: request when a table has an
unresolved fail-level data-quality issue with a known source_folder.

This confirms that wants_visualization=True inherits the auto-clean redirect
behavior from add_context / clean_and_reload, exactly as a normal question does.

Uses the same fixture infrastructure as test_auto_clean_redirect.py.

Run:
  printf 'yes\\nyes\\n' | uv run python -m tests.test_visualize_auto_clean
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

FAIL_FIXTURE_FOLDER = "data/_test_etl/dq_fail"
FAIL_CSV_NAME = "dq_fail_orders.csv"
FAIL_TABLE = "dq_fail_orders"


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeDedupLLM:
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


def _setup_fail_table(conn):
    load_csv_to_table(conn, Path(FAIL_FIXTURE_FOLDER) / FAIL_CSV_NAME)
    issues_found = [
        {"issue": "Duplicate rows: 1 fully duplicate rows found.", "severity": "fail"},
        {"issue": "Duplicate values: column 'order_id' looks like a unique identifier but has 1 duplicate value.", "severity": "fail"},
    ]
    write_data_quality_status(
        conn, FAIL_TABLE, "fail", issues_found, was_cleaned=False,
        source_folder=FAIL_FIXTURE_FOLDER,
    )


def _cleanup(conn):
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{FAIL_TABLE}" CASCADE')
        cur.execute("DELETE FROM _data_quality_status WHERE table_name = %s", (FAIL_TABLE,))
    conn.commit()


print("=" * 70)
print("TEST: visualize request -> auto-clean redirect fires before visualization")
print("(pipe 'yes\\nyes\\n' to stdin)")
print("=" * 70)

conn = get_admin_connection()
try:
    ensure_data_quality_status_table(conn)
    _setup_fail_table(conn)

    # State has wants_visualization=True (as visualize_node would set it)
    state = SQLAnalystState(
        wants_visualization=True,
        user_question=f"Show me a bar chart of how many rows are in {FAIL_TABLE}.",
        curated_question=f"Show a bar chart of the number of rows in {FAIL_TABLE}.",
        cleaning_attempted_tables=[],
    )

    # First add_context: should detect the fail-level table and set needs_cleaning
    ctx1 = add_context(state)
    state = state.model_copy(update=ctx1)

    print(f"After first add_context:")
    print(f"  data_quality_action: {state.data_quality_action}")
    print(f"  tables_to_clean: {state.tables_to_clean}")
    print(f"  wants_visualization: {state.wants_visualization}")

    assert state.data_quality_action == "needs_cleaning", (
        f"expected 'needs_cleaning' for a fail-level table, got {state.data_quality_action!r}"
    )
    assert any(item["table"] == FAIL_TABLE for item in state.tables_to_clean), (
        f"expected {FAIL_TABLE} in tables_to_clean"
    )
    assert state.wants_visualization is True, "wants_visualization must survive through add_context"
    print("PASS: add_context correctly sets needs_cleaning for a visualize request.\n")

    # clean_and_reload fires (FakeDedupLLM + piped 'yes yes' via stdin)
    print("Running clean_and_reload...")
    reload_result = clean_and_reload(state, _llm=FakeDedupLLM())
    state = state.model_copy(update=reload_result)

    print(f"cleaning_attempted_tables: {state.cleaning_attempted_tables}")
    assert FAIL_TABLE in state.cleaning_attempted_tables, (
        f"expected {FAIL_TABLE} in cleaning_attempted_tables"
    )
    print("PASS: clean_and_reload fired exactly as it does for a normal question.\n")

    # Second add_context: should now proceed (cleaning resolved the issue)
    ctx2 = add_context(state)
    state = state.model_copy(update=ctx2)

    print(f"After second add_context:")
    print(f"  data_quality_action: {state.data_quality_action}")
    print(f"  wants_visualization: {state.wants_visualization}")

    # route_after_add_context with wants_visualization=True and action="proceed"
    # should route to "determine_chart_type" — verify the state is in the right shape
    from agents.sql_analyst import route_after_add_context
    route_key = route_after_add_context(state)
    print(f"  route_after_add_context returns: {route_key!r}")

    assert state.data_quality_action == "proceed", (
        f"expected 'proceed' after successful cleaning, got {state.data_quality_action!r}"
    )
    assert state.wants_visualization is True, "wants_visualization must survive through all nodes"
    assert route_key == "determine_chart_type", (
        f"expected 'determine_chart_type' for a visualize request after cleaning, got {route_key!r}"
    )
    print("PASS: after cleaning, route_after_add_context -> 'determine_chart_type' (not 'generate_sql').\n")

finally:
    _cleanup(conn)
    conn.close()

print("=" * 70)
print("VISUALIZE AUTO-CLEAN REDIRECT TEST PASSED")
print("=" * 70)
