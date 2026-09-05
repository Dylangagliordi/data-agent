"""
Confirm that execute_sql kills a deliberately slow/expensive query via the
PostgreSQL statement_timeout instead of letting it run unbounded.

The test temporarily shortens _STATEMENT_TIMEOUT_MS to 500ms, then runs a
query that sleeps for 10 seconds.  The timeout must fire before that completes
and return a SQL_EXECUTION_ERROR with a QueryCanceled or timeout message.

This guards against the class of queries that must fully execute before
returning any rows (e.g. a sort over an unindexed column on a huge table) —
the MAX_RESULT_ROWS cap alone cannot stop those, since rows are only available
after the full execution finishes.

Architecture review point #26: `SET LOCAL statement_timeout` only binds within
an open transaction — under autocommit=True, every cur.execute() is its own
implicit transaction, so a timeout set on one statement would never apply to
the next (the real query) at all. STEP 0 below opens a real connection via
get_app_reader_connection() and asserts autocommit is actually False on it
(psycopg2's own default, never overridden by that helper) — not just assumed
from reading the source — before the timeout-kill assertion is trusted as
proof the fix genuinely works under this connection's real, current
configuration. execute_sql itself also defensively forces autocommit off if
it were ever somehow on (see its own comment) — this test would still catch
a regression there, since it exercises the real function end-to-end.
"""

import agents.sql_analyst as _sql_analyst_mod
from agents.sql_analyst import _SQL_ERROR_PREFIX, execute_sql
from models.schema import SQLAnalystState
from utils.db import get_app_reader_connection

if __name__ == "__main__":
    print("=" * 70)
    print("STEP 0: confirm the real connection's actual autocommit setting")
    print("=" * 70)

    probe_conn = get_app_reader_connection()
    try:
        print(f"conn.autocommit = {probe_conn.autocommit!r}")
        assert probe_conn.autocommit is False, (
            "get_app_reader_connection() must NOT be in autocommit mode — otherwise "
            "SET LOCAL statement_timeout (set on one cur.execute() call) would never "
            "apply to the next cur.execute() call (the real query), since each would "
            "be its own separate implicit transaction."
        )
        print("PASSED: connection is not in autocommit mode — SET LOCAL genuinely applies.\n")
    finally:
        probe_conn.close()

    print("=" * 70)
    print("Statement timeout — slow query must be killed before completing")
    print("=" * 70)

    original_timeout = _sql_analyst_mod._STATEMENT_TIMEOUT_MS
    _sql_analyst_mod._STATEMENT_TIMEOUT_MS = 500  # 0.5 seconds for fast testing

    try:
        state = SQLAnalystState(
            generated_sql_query="SELECT pg_sleep(10)",  # would take 10s without timeout
        )
        result = execute_sql(state)
        raw = result["sql_query_execution_result"]
        print(f"result: {raw[:200]}")

        assert raw.startswith(_SQL_ERROR_PREFIX), (
            f"expected an error string starting with {_SQL_ERROR_PREFIX!r}, got: {raw[:100]!r}"
        )
        raw_lower = raw.lower()
        assert "cancel" in raw_lower or "timeout" in raw_lower, (
            f"expected 'cancel' or 'timeout' in error, got: {raw[:200]!r}"
        )
        print("PASSED: slow query was killed by statement_timeout.")
    finally:
        _sql_analyst_mod._STATEMENT_TIMEOUT_MS = original_timeout
        print(f"Restored _STATEMENT_TIMEOUT_MS to {original_timeout}ms.")

    print("\n" + "=" * 70)
    print("STEP 2: Normal fast query still completes within the timeout")
    print("=" * 70)

    state_fast = SQLAnalystState(
        generated_sql_query="SELECT 1 AS n"
    )
    result_fast = execute_sql(state_fast)
    raw_fast = result_fast["sql_query_execution_result"]
    print(f"fast result: {raw_fast}")
    assert not raw_fast.startswith(_SQL_ERROR_PREFIX), (
        f"fast query should succeed, got: {raw_fast[:100]}"
    )

    import json
    data = json.loads(raw_fast)
    assert data["rows"] == [[1]], f"expected [[1]], got {data['rows']}"
    print("PASSED: fast query completes normally within the timeout.")

    print("\n" + "=" * 70)
    print("STEP 3: even if a future connection came back with autocommit=True,")
    print("execute_sql's own defensive guard must still make the timeout apply")
    print("=" * 70)

    _sql_analyst_mod._STATEMENT_TIMEOUT_MS = 500
    original_get_conn = _sql_analyst_mod.get_app_reader_connection

    def _autocommit_true_connection():
        conn = original_get_conn()
        conn.autocommit = True  # simulate a hypothetical future regression
        return conn

    _sql_analyst_mod.get_app_reader_connection = _autocommit_true_connection
    try:
        state_autocommit = SQLAnalystState(generated_sql_query="SELECT pg_sleep(10)")
        result_autocommit = execute_sql(state_autocommit)
        raw_autocommit = result_autocommit["sql_query_execution_result"]
        print(f"result under forced autocommit=True: {raw_autocommit[:200]}")

        assert raw_autocommit.startswith(_SQL_ERROR_PREFIX), (
            "even with autocommit forced True on the connection, execute_sql's own "
            "guard must disable it before SET LOCAL — otherwise this slow query would "
            f"run unbounded instead of being killed. Got: {raw_autocommit[:200]!r}"
        )
        raw_autocommit_lower = raw_autocommit.lower()
        assert "cancel" in raw_autocommit_lower or "timeout" in raw_autocommit_lower, (
            f"expected 'cancel' or 'timeout' in error, got: {raw_autocommit[:200]!r}"
        )
        print("PASSED: execute_sql's defensive autocommit guard made the timeout apply "
              "even when the connection came back with autocommit=True.")
    finally:
        _sql_analyst_mod.get_app_reader_connection = original_get_conn
        _sql_analyst_mod._STATEMENT_TIMEOUT_MS = original_timeout

    print("\n" + "=" * 70)
    print("ALL ASSERTIONS PASSED")
    print("=" * 70)
