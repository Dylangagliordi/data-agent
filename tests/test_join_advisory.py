"""
Tests for Spec 9, Part 2: Join Advisory (utils/join_advisory.py).

Real, temporary tables created directly via the admin connection (same
pattern as tests/test_fanout_status.py) — a genuine declared FOREIGN KEY
relationship, and a genuine name-matched INFERRED relationship (no declared
constraint at all, built from the real cardinality-heuristic detection
compute_and_write_fanout_status already proves out in test_fanout_status.py).

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_join_advisory.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.join_advisory import get_join_advisory, render_join_advisory_html
from utils.load_data import compute_and_write_fanout_status, ensure_fanout_status_table, get_admin_connection

T_PRODUCTS = "_test_spec9_products"
T_REVIEWS = "_test_spec9_reviews"
T_CUSTOMERS = "_test_spec9_customers"
T_ORDERS = "_test_spec9_orders"
ALL_TABLES = [T_PRODUCTS, T_REVIEWS, T_CUSTOMERS, T_ORDERS]


def _cleanup(conn):
    with conn.cursor() as cur:
        for t in [T_REVIEWS, T_PRODUCTS, T_ORDERS, T_CUSTOMERS]:
            cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE;')
        cur.execute(
            "DELETE FROM _fanout_status WHERE table_name = ANY(%s)", (ALL_TABLES,)
        )
    conn.commit()


def test_declared_and_inferred_relationships():
    conn = get_admin_connection()
    ensure_fanout_status_table(conn)
    _cleanup(conn)
    try:
        # A real declared FOREIGN KEY — ground truth from information_schema,
        # independent of _fanout_status.
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE "{T_PRODUCTS}" (product_id TEXT PRIMARY KEY, name TEXT);')
            cur.execute(
                f'CREATE TABLE "{T_REVIEWS}" (review_id TEXT, '
                f'product_id TEXT REFERENCES "{T_PRODUCTS}"(product_id));'
            )
            cur.executemany(f'INSERT INTO "{T_PRODUCTS}" VALUES (%s, %s)', [("p1", "Widget")])
            cur.executemany(
                f'INSERT INTO "{T_REVIEWS}" VALUES (%s, %s)', [("r1", "p1"), ("r2", "p1")]
            )
        conn.commit()
        compute_and_write_fanout_status(conn, T_PRODUCTS)
        compute_and_write_fanout_status(conn, T_REVIEWS)

        # A genuine name-matched relationship with NO declared constraint at
        # all: customer_id is unique in customers (PK-like), repeats in
        # orders (FK-like, real fan-out) — exactly test_fanout_status.py's
        # own scenario 2/3 setup, reused here for a second, independent table
        # pair so it can never be confused with the declared one above.
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE "{T_CUSTOMERS}" (customer_id TEXT, name TEXT);')
            cur.execute(f'CREATE TABLE "{T_ORDERS}" (order_id TEXT, customer_id TEXT);')
            cur.executemany(
                f'INSERT INTO "{T_CUSTOMERS}" VALUES (%s, %s)', [("c1", "Alice"), ("c2", "Bob")]
            )
            cur.executemany(
                f'INSERT INTO "{T_ORDERS}" VALUES (%s, %s)',
                [("o1", "c1"), ("o2", "c1"), ("o3", "c2")],
            )
        conn.commit()
        compute_and_write_fanout_status(conn, T_CUSTOMERS)
        compute_and_write_fanout_status(conn, T_ORDERS)

        advisory = get_join_advisory()

        declared_match = next(
            (
                r for r in advisory["declared"]
                if r["table"] == T_REVIEWS and r["column"] == "product_id" and r["referenced_table"] == T_PRODUCTS
            ),
            None,
        )
        assert declared_match is not None, f"expected a declared FK for {T_REVIEWS}.product_id, got {advisory['declared']}"
        assert declared_match["has_fanout"] is True, "product_id appears twice in reviews, so a declared FK must still show real fan-out"

        inferred_match = next(
            (
                r for r in advisory["inferred"]
                if r["table"] == T_ORDERS and r["column"] == "customer_id" and r["referenced_table"] == T_CUSTOMERS
            ),
            None,
        )
        assert inferred_match is not None, (
            f"expected an inferred relationship for {T_ORDERS}.customer_id -> {T_CUSTOMERS}, "
            f"got {advisory['inferred']}"
        )
        assert inferred_match["has_fanout"] is True, "customer_id repeats in orders, so this must show real fan-out"

        # The declared relationship must never ALSO be double-listed as inferred.
        assert not any(
            r["table"] == T_REVIEWS and r["column"] == "product_id" for r in advisory["inferred"]
        ), "a declared relationship must never be double-counted as inferred"

        print("PASS: get_join_advisory surfaces a real declared FK and a real inferred relationship, with no double-counting")
    finally:
        _cleanup(conn)
        conn.close()


def test_render_join_advisory_html():
    conn = get_admin_connection()
    ensure_fanout_status_table(conn)
    _cleanup(conn)
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE TABLE "{T_PRODUCTS}" (product_id TEXT PRIMARY KEY);')
            cur.execute(
                f'CREATE TABLE "{T_REVIEWS}" (review_id TEXT, product_id TEXT REFERENCES "{T_PRODUCTS}"(product_id));'
            )
        conn.commit()
        compute_and_write_fanout_status(conn, T_PRODUCTS)
        compute_and_write_fanout_status(conn, T_REVIEWS)

        path = render_join_advisory_html()
        content = open(path).read()
        assert T_REVIEWS in content and T_PRODUCTS in content
        assert "Declared foreign keys" in content
        assert "Inferred relationships" in content
        print(f"PASS: render_join_advisory_html renders a real relationship map: {path}")
    finally:
        _cleanup(conn)
        conn.close()


if __name__ == "__main__":
    test_declared_and_inferred_relationships()
    test_render_join_advisory_html()
    print("\nAll join_advisory tests passed.")
