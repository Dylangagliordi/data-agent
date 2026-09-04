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
"""

import agents.sql_analyst as _sql_analyst_mod
from agents.sql_analyst import _SQL_ERROR_PREFIX, execute_sql
from models.schema import SQLAnalystState

if __name__ == "__main__":
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
    print("ALL ASSERTIONS PASSED")
    print("=" * 70)
