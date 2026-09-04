"""Tests for precomputed fan-out metadata (_fanout_status).

Five scenarios:
1. load_data.py populates _fanout_status → add_context reads from it without running
   a live COUNT(DISTINCT) scan for covered tables.
2. Id-like column unique within its own table → source='cardinality_heuristic',
   is_likely_fk=False, has_fanout=False (inferred PK, no warning).
3. Id-like column with genuine fan-out, no declared constraint →
   source='cardinality_heuristic', is_likely_fk=True, has_fanout=True (warning fires).
4. Declared FOREIGN KEY column with fan-out → source='declared_fk',
   is_likely_fk=True, has_fanout=True.
5. Table with no _fanout_status rows → live-check fallback, logged to stderr,
   warning still surfaces in add_context output.

All test tables are created directly via the admin connection and cleaned up in the
finally block. No real CSV loading is required.

Run with:
    uv run python -m tests.test_fanout_status
"""

import io
import sys
from agents.sql_analyst import add_context, _read_fanout_from_metadata
from models.schema import SQLAnalystState
from utils.load_data import (
    compute_and_write_fanout_status,
    ensure_fanout_status_table,
    get_admin_connection,
)


def fetch_fanout_rows(conn, table_name: str) -> list:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, is_likely_fk, has_fanout, source
            FROM _fanout_status
            WHERE table_name = %s
            ORDER BY column_name
            """,
            (table_name,),
        )
        return cur.fetchall()


def cleanup(conn, *table_names: str) -> None:
    with conn.cursor() as cur:
        for t in table_names:
            cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE;')
            cur.execute("DELETE FROM _fanout_status WHERE table_name = %s", (t,))
    conn.commit()


conn = get_admin_connection()
ensure_fanout_status_table(conn)

created = []

try:
    # ── Scenario 1 + 2: inferred PK (unique id-like column, no warning) ────────
    print("=" * 70)
    print("SCENARIO 1+2: unique id-like column → inferred PK, no fan-out warning")
    print("=" * 70)
    t_customers = "tf_customers"
    created.append(t_customers)
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{t_customers}" CASCADE;')
        cur.execute(f'CREATE TABLE "{t_customers}" (customer_id TEXT, name TEXT);')
        cur.executemany(
            f'INSERT INTO "{t_customers}" VALUES (%s, %s)',
            [("c1", "Alice"), ("c2", "Bob"), ("c3", "Carol")],
        )
    conn.commit()

    compute_and_write_fanout_status(conn, t_customers)
    rows = fetch_fanout_rows(conn, t_customers)
    print("_fanout_status rows:", rows)

    assert len(rows) == 1, f"expected 1 row for customer_id, got {rows}"
    col, is_fk, has_fanout, source = rows[0]
    assert col == "customer_id", f"unexpected column: {col}"
    assert is_fk is False, f"unique id-like column should NOT be is_likely_fk, got {is_fk}"
    assert has_fanout is False, f"unique id-like column should NOT have fan-out, got {has_fanout}"
    assert source == "cardinality_heuristic", f"expected cardinality_heuristic, got {source}"
    print("PASS: unique customer_id → is_likely_fk=False, has_fanout=False, source='cardinality_heuristic'\n")

    # Confirm add_context emits NO WARNING for this table
    state = SQLAnalystState(user_question=f"How many rows in {t_customers}?",
                            curated_question=f"How many rows in {t_customers}?")
    ctx = add_context(state)
    ctx_text = ctx["prompt_query_context"]
    warning_lines = [ln for ln in ctx_text.splitlines()
                     if ln.startswith("WARNING") and t_customers in ln and "fan-out" in ln]
    assert warning_lines == [], f"expected no fan-out warning for unique-id table, got: {warning_lines}"
    print("PASS: add_context emits no fan-out warning for unique-id table.\n")

    # ── Scenario 3: cardinality_heuristic FK with genuine fan-out ──────────────
    print("=" * 70)
    print("SCENARIO 3: non-unique id-like column → inferred FK, fan-out warning")
    print("=" * 70)
    t_orders = "tf_orders"
    created.append(t_orders)
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{t_orders}" CASCADE;')
        cur.execute(f'CREATE TABLE "{t_orders}" (order_id TEXT, item TEXT);')
        cur.executemany(
            f'INSERT INTO "{t_orders}" VALUES (%s, %s)',
            [("o1", "apple"), ("o1", "banana"), ("o1", "cherry"), ("o2", "date")],
        )
    conn.commit()

    compute_and_write_fanout_status(conn, t_orders)
    rows = fetch_fanout_rows(conn, t_orders)
    print("_fanout_status rows:", rows)

    assert len(rows) == 1, f"expected 1 row for order_id, got {rows}"
    col, is_fk, has_fanout, source = rows[0]
    assert col == "order_id", f"unexpected column: {col}"
    assert is_fk is True, f"non-unique id-like column should be is_likely_fk, got {is_fk}"
    assert has_fanout is True, f"non-unique id-like column should have fan-out, got {has_fanout}"
    assert source == "cardinality_heuristic", f"expected cardinality_heuristic, got {source}"
    print("PASS: non-unique order_id → is_likely_fk=True, has_fanout=True, source='cardinality_heuristic'\n")

    # Confirm add_context reads metadata (no live check since we just populated it)
    # and emits the fan-out warning
    state = SQLAnalystState(user_question=f"How many items per order in {t_orders}?",
                            curated_question=f"How many items per order in {t_orders}?")
    ctx = add_context(state)
    ctx_text = ctx["prompt_query_context"]
    warning_lines = [ln for ln in ctx_text.splitlines()
                     if ln.startswith("WARNING") and t_orders in ln and "fan-out" in ln]
    assert warning_lines, f"expected a fan-out warning for {t_orders}, got none"
    assert "inferred from data distribution" in warning_lines[0], (
        f"expected 'inferred from data distribution' label, got: {warning_lines[0]}"
    )
    print("PASS: add_context surfaces fan-out warning from _fanout_status metadata.")
    print("  Warning:", warning_lines[0], "\n")

    # ── Scenario 4: declared FOREIGN KEY ───────────────────────────────────────
    print("=" * 70)
    print("SCENARIO 4: declared FOREIGN KEY column with fan-out → source='declared_fk'")
    print("=" * 70)
    t_products = "tf_products"
    t_reviews = "tf_reviews"
    created.extend([t_products, t_reviews])
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{t_reviews}" CASCADE;')
        cur.execute(f'DROP TABLE IF EXISTS "{t_products}" CASCADE;')
        cur.execute(f'CREATE TABLE "{t_products}" (product_id TEXT PRIMARY KEY, name TEXT);')
        cur.execute(
            f'CREATE TABLE "{t_reviews}" ('
            f'  review_id TEXT, '
            f'  product_id TEXT REFERENCES "{t_products}"(product_id)'
            f');'
        )
        cur.executemany(
            f'INSERT INTO "{t_products}" VALUES (%s, %s)',
            [("p1", "Widget"), ("p2", "Gadget")],
        )
        cur.executemany(
            f'INSERT INTO "{t_reviews}" VALUES (%s, %s)',
            [("r1", "p1"), ("r2", "p1"), ("r3", "p2")],
        )
    conn.commit()

    compute_and_write_fanout_status(conn, t_reviews)
    rows = fetch_fanout_rows(conn, t_reviews)
    print("_fanout_status rows for tf_reviews:", rows)

    # product_id is a declared FK; review_id is not id-like
    fk_row = next((r for r in rows if r[0] == "product_id"), None)
    assert fk_row is not None, f"expected a _fanout_status row for product_id, got {rows}"
    col, is_fk, has_fanout, source = fk_row
    assert is_fk is True, f"declared FK should be is_likely_fk=True, got {is_fk}"
    assert has_fanout is True, f"product_id appears multiple times so has_fanout should be True, got {has_fanout}"
    assert source == "declared_fk", f"expected source='declared_fk', got {source!r}"
    print("PASS: declared FK product_id → is_likely_fk=True, has_fanout=True, source='declared_fk'\n")

    # Verify add_context warning says "declared foreign key"
    state = SQLAnalystState(user_question=f"How many reviews per product in {t_reviews}?",
                            curated_question=f"How many reviews per product in {t_reviews}?")
    ctx = add_context(state)
    ctx_text = ctx["prompt_query_context"]
    warning_lines = [ln for ln in ctx_text.splitlines()
                     if ln.startswith("WARNING") and t_reviews in ln and "fan-out" in ln]
    assert warning_lines, f"expected a fan-out warning for {t_reviews}"
    assert "declared foreign key" in warning_lines[0], (
        f"expected 'declared foreign key' label, got: {warning_lines[0]}"
    )
    print("PASS: add_context surfaces declared-FK warning with correct label.")
    print("  Warning:", warning_lines[0], "\n")

    # ── Scenario 5: no _fanout_status entry → live fallback logged to stderr ───
    print("=" * 70)
    print("SCENARIO 5: table missing from _fanout_status → live-check fallback")
    print("=" * 70)
    t_live = "tf_live_fallback"
    created.append(t_live)
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{t_live}" CASCADE;')
        cur.execute(f'CREATE TABLE "{t_live}" (order_id TEXT, qty INT);')
        cur.executemany(
            f'INSERT INTO "{t_live}" VALUES (%s, %s)',
            [("o1", 3), ("o1", 5), ("o2", 1)],
        )
    conn.commit()
    # Deliberately do NOT call compute_and_write_fanout_status — no metadata row

    buf = io.StringIO()
    old_stderr = sys.stderr
    sys.stderr = buf
    try:
        state = SQLAnalystState(user_question=f"Show orders in {t_live}",
                                curated_question=f"Show orders in {t_live}")
        ctx = add_context(state)
    finally:
        sys.stderr = old_stderr

    stderr_out = buf.getvalue()
    ctx_text = ctx["prompt_query_context"]

    assert t_live in stderr_out and "falling back to live check" in stderr_out, (
        f"expected fallback log in stderr, got: {stderr_out!r}"
    )
    print("PASS: stderr logged fallback message:", stderr_out.strip())

    warning_lines = [ln for ln in ctx_text.splitlines()
                     if ln.startswith("WARNING") and t_live in ln and "fan-out" in ln]
    assert warning_lines, f"expected live-check fan-out warning for {t_live}"
    print("PASS: live-check fallback correctly surfaces fan-out warning.")
    print("  Warning:", warning_lines[0], "\n")

    print("=" * 70)
    print("ALL FANOUT-STATUS ASSERTIONS PASSED")
    print("=" * 70)

finally:
    cleanup(conn, *created)
    conn.close()
