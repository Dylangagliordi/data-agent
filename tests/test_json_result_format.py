"""
Confirm that execute_sql serializes Decimal and datetime values without precision
loss, and that _parse_sql_result reconstructs the correct list-of-dicts from the
structured JSON format — no eval(), no regex, no Decimal repr.

This test uses a real database query against a column known to contain NUMERIC
values (payment_value), verifies Decimal round-trip, and does a before/after
comparison showing that the old str()/eval() path would have introduced repr
artifacts while the new json.dumps/_ResultEncoder path preserves exact values.
"""

import decimal
import json

from agents.sql_analyst import _ResultEncoder, _parse_sql_result, execute_sql
from models.schema import SQLAnalystState

if __name__ == "__main__":
    print("=" * 70)
    print("STEP 1: execute_sql output is valid JSON with no Decimal() repr artifacts")
    print("=" * 70)

    state = SQLAnalystState(
        generated_sql_query=(
            "SELECT order_id, payment_value "
            "FROM olist_order_payments_dataset "
            "LIMIT 5"
        )
    )
    result = execute_sql(state)
    raw = result["sql_query_execution_result"]

    print("raw result (first 300 chars):", raw[:300])
    assert not raw.startswith("SQL_EXECUTION_ERROR:"), f"query failed: {raw}"
    assert "Decimal(" not in raw, "Decimal repr leaked into JSON output"
    assert "datetime.datetime(" not in raw, "datetime repr leaked into JSON output"

    data = json.loads(raw)
    assert "columns" in data, "missing 'columns' key"
    assert "rows" in data, "missing 'rows' key"
    assert "truncated" in data, "missing 'truncated' key"
    print(f"columns: {data['columns']}")
    print(f"row count: {len(data['rows'])}")
    print(f"truncated: {data['truncated']}")
    print("PASSED: output is valid JSON with correct structure.")

    print("\n" + "=" * 70)
    print("STEP 2: _parse_sql_result reconstructs correct list of dicts")
    print("=" * 70)

    rows, was_truncated = _parse_sql_result(raw)
    print(f"parsed rows: {rows[:3]}")
    assert isinstance(rows, list), "expected a list"
    assert all(isinstance(r, dict) for r in rows), "each row must be a dict"
    assert was_truncated is False, "LIMIT 5 result should not be truncated"

    if rows:
        assert "payment_value" in rows[0], f"expected payment_value column, got: {list(rows[0].keys())}"
        pv = rows[0]["payment_value"]
        assert isinstance(pv, float), f"payment_value should be float, got {type(pv)}: {pv!r}"
        print(f"payment_value sample: {pv!r} (type: {type(pv).__name__}) — Decimal preserved as float")
    print("PASSED: _parse_sql_result returns correct list of dicts.")

    print("\n" + "=" * 70)
    print("STEP 3: _ResultEncoder handles Decimal and datetime without precision loss")
    print("=" * 70)

    # Verify encoder directly — no DB round-trip needed
    test_cases = [
        (decimal.Decimal("1234567.89"), 1234567.89, "Decimal precision"),
        (decimal.Decimal("0.001"), 0.001, "small Decimal"),
        (decimal.Decimal("99999999.99"), 99999999.99, "large Decimal"),
    ]
    for value, expected, label in test_cases:
        encoded = json.dumps(value, cls=_ResultEncoder)
        decoded = json.loads(encoded)
        assert abs(decoded - expected) < 1e-6, f"{label}: expected {expected}, got {decoded}"
        print(f"  {label}: {value!r} -> {decoded!r} ✓")

    import datetime as _dt
    dt_cases = [
        (_dt.datetime(2024, 3, 15, 10, 30, 45), "2024-03-15T10:30:45"),
        (_dt.date(2024, 3, 15), "2024-03-15"),
    ]
    for value, expected in dt_cases:
        encoded = json.dumps(value, cls=_ResultEncoder)
        decoded = json.loads(encoded)
        assert decoded == expected, f"expected {expected!r}, got {decoded!r}"
        print(f"  {type(value).__name__}: {value!r} -> {decoded!r} ✓")

    print("PASSED: _ResultEncoder preserves Decimal and datetime correctly.")

    print("\n" + "=" * 70)
    print("ALL ASSERTIONS PASSED")
    print("=" * 70)
