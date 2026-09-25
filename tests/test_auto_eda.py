"""
Tests for Spec 9, Part 1: Auto-EDA (utils/auto_eda.py).

Creates a real, temporary table directly via the admin connection (same
pattern as tests/test_fanout_status.py) with hand-known values, so every
computed statistic can be checked against a value worked out by hand rather
than just "did it run without crashing." Reads back via profile_table's own
real app_reader (read-only) connection — proving the numbers are actually
reachable through the same path add_context and every other reader use, not
just visible to the admin connection that created them.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_auto_eda.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.auto_eda import profile_table, render_auto_eda_html
from utils.load_data import get_admin_connection

TABLE_NAME = "_test_spec9_auto_eda"


def _seed_table(conn):
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{TABLE_NAME}" CASCADE;')
        cur.execute(
            f'CREATE TABLE "{TABLE_NAME}" (id INTEGER, rating NUMERIC, industry TEXT);'
        )
        # rating: 10, 20, 30, 40, NULL -> avg of non-null = 25, 1 null (20%).
        # industry: Tech x2, Retail x2, Healthcare x1, NULL x0.
        rows = [
            (1, 10, "Tech"),
            (2, 20, "Tech"),
            (3, 30, "Retail"),
            (4, 40, "Retail"),
            (5, None, "Healthcare"),
        ]
        cur.executemany(
            f'INSERT INTO "{TABLE_NAME}" (id, rating, industry) VALUES (%s, %s, %s)', rows
        )
    conn.commit()


def _cleanup(conn):
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{TABLE_NAME}" CASCADE;')
    conn.commit()


def test_profile_table_real_numeric_and_categorical_stats():
    conn = get_admin_connection()
    _seed_table(conn)
    try:
        profile = profile_table(TABLE_NAME)
        assert profile["table_name"] == TABLE_NAME
        assert profile["row_count"] == 5

        by_name = {c["name"]: c for c in profile["columns"]}

        rating = by_name["rating"]
        assert rating["kind"] == "numeric"
        assert rating["null_count"] == 1
        assert abs(rating["null_rate"] - 0.2) < 1e-9
        assert rating["distinct_count"] == 4
        assert float(rating["min"]) == 10
        assert float(rating["max"]) == 40
        assert abs(rating["avg"] - 25.0) < 1e-9

        industry = by_name["industry"]
        assert industry["kind"] == "categorical"
        assert industry["null_count"] == 0
        assert industry["distinct_count"] == 3
        top_by_value = {v["value"]: v["count"] for v in industry["top_values"]}
        assert top_by_value == {"Tech": 2, "Retail": 2, "Healthcare": 1}
        assert industry["is_high_cardinality"] is False  # only 3 distinct values

        id_col = by_name["id"]
        assert id_col["kind"] == "numeric"
        assert id_col["null_count"] == 0
        assert id_col["distinct_count"] == 5

        print("PASS: profile_table computes real, hand-verifiable numeric and categorical statistics")
    finally:
        _cleanup(conn)
        conn.close()


def test_render_auto_eda_html():
    conn = get_admin_connection()
    _seed_table(conn)
    try:
        path = render_auto_eda_html(TABLE_NAME)
        content = open(path).read()
        assert "rating" in content and "industry" in content
        assert "25.00" in content  # the real computed average
        assert "Tech (2)" in content
        print(f"PASS: render_auto_eda_html renders real computed statistics: {path}")
    finally:
        _cleanup(conn)
        conn.close()


def test_high_cardinality_flag_reuses_categorical_consolidation_thresholds():
    conn = get_admin_connection()
    with conn.cursor() as cur:
        cur.execute(f'DROP TABLE IF EXISTS "{TABLE_NAME}" CASCADE;')
        cur.execute(f'CREATE TABLE "{TABLE_NAME}" (label TEXT);')
        # 20 distinct labels across 40 rows -> ratio 0.5, distinct_count 20:
        # both clear the real thresholds (MIN_DISTINCT=15, 0.03 <= ratio <= 0.9).
        rows = [(f"label_{i % 20}",) for i in range(40)]
        cur.executemany(f'INSERT INTO "{TABLE_NAME}" (label) VALUES (%s)', rows)
    conn.commit()
    try:
        profile = profile_table(TABLE_NAME)
        label_col = next(c for c in profile["columns"] if c["name"] == "label")
        assert label_col["distinct_count"] == 20
        assert label_col["is_high_cardinality"] is True
        print("PASS: high-cardinality flag fires using the real, shared categorical_consolidation thresholds")
    finally:
        _cleanup(conn)
        conn.close()


if __name__ == "__main__":
    test_profile_table_real_numeric_and_categorical_stats()
    test_render_auto_eda_html()
    test_high_cardinality_flag_reuses_categorical_consolidation_thresholds()
    print("\nAll auto_eda tests passed.")
