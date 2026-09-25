"""
Tests for Spec 9, Part 3: Significance Testing (Rule 15) —
agents/sql_analyst.py:_significance_test_note, wired into
_analyst_judgment_disclosure.

No DB, no LLM for the core unit tests — pure math against hand-built result
rows. A final live-data test queries the real olist database (app_reader,
read-only) to prove the whole thing works end to end against genuine data.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_significance_testing.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents.sql_analyst import _analyst_judgment_disclosure, _significance_test_note


def test_clearly_significant_difference():
    # Tight variance, well-separated means, healthy sample sizes -> significant.
    result_data = [
        {"industry": "Tech", "avg_rating": 4.5, "stddev_rating": 0.2, "count": 50},
        {"industry": "Retail", "avg_rating": 3.0, "stddev_rating": 0.2, "count": 50},
        {"industry": "Healthcare", "avg_rating": 3.8, "stddev_rating": 0.2, "count": 50},
    ]
    note = _significance_test_note(result_data)
    assert "statistically significant difference" in note
    assert "no statistically significant" not in note
    assert "p=" in note and "F=" in note
    print(f"PASS: clearly separated groups are flagged as statistically significant: {note!r}")


def test_no_significant_difference():
    # Nearly identical means with high variance relative to the difference -> not significant.
    result_data = [
        {"industry": "Tech", "avg_rating": 3.51, "stddev_rating": 2.5, "count": 20},
        {"industry": "Retail", "avg_rating": 3.49, "stddev_rating": 2.5, "count": 20},
    ]
    note = _significance_test_note(result_data)
    assert "no statistically significant difference" in note
    print(f"PASS: near-identical noisy groups are correctly flagged as not significant: {note!r}")


def test_missing_stddev_or_count_never_fabricates_a_result():
    only_mean = [
        {"industry": "Tech", "avg_rating": 4.5},
        {"industry": "Retail", "avg_rating": 3.0},
    ]
    assert _significance_test_note(only_mean) == ""

    mean_and_count_no_stddev = [
        {"industry": "Tech", "avg_rating": 4.5, "count": 50},
        {"industry": "Retail", "avg_rating": 3.0, "count": 50},
    ]
    assert _significance_test_note(mean_and_count_no_stddev) == ""
    print("PASS: never runs (or fakes) a test when the required stddev/count ingredients are missing")


def test_single_group_and_empty_result():
    assert _significance_test_note([]) == ""
    assert _significance_test_note(
        [{"industry": "Tech", "avg_rating": 4.5, "stddev_rating": 0.2, "count": 50}]
    ) == ""
    print("PASS: an empty result or a single group never produces a fabricated test")


def test_wired_into_analyst_judgment_disclosure():
    sql = "SELECT industry, AVG(rating) AS avg_rating, STDDEV(rating) AS stddev_rating, COUNT(*) AS count FROM jobs GROUP BY industry"
    result_data = [
        {"industry": "Tech", "avg_rating": 4.5, "stddev_rating": 0.2, "count": 50},
        {"industry": "Retail", "avg_rating": 3.0, "stddev_rating": 0.2, "count": 50},
    ]
    disclosure = _analyst_judgment_disclosure(sql, result_data)
    assert "one-way ANOVA" in disclosure
    print("PASS: _analyst_judgment_disclosure surfaces the significance note when the real ingredients are present")


def test_against_real_live_data():
    from utils.db import get_app_reader_connection

    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT customer_state,
                       AVG(payment_value) AS avg_payment_value,
                       STDDEV(payment_value) AS stddev_payment_value,
                       COUNT(*) AS count
                FROM olist_order_payments_dataset p
                JOIN olist_orders_dataset o ON p.order_id = o.order_id
                JOIN olist_customers_dataset c ON o.customer_id = c.customer_id
                GROUP BY customer_state
                HAVING COUNT(*) >= 30
                ORDER BY avg_payment_value DESC
                """
            )
            rows = cur.fetchall()
            col_names = [d[0] for d in cur.description]
        conn.rollback()
    finally:
        conn.close()

    result_data = [dict(zip(col_names, r)) for r in rows]
    assert len(result_data) >= 2, "need real live data with at least 2 qualifying states to run this test"

    note = _significance_test_note(result_data)
    assert note != "", "real olist payment data across states should have enough groups/samples to run the test"
    assert ("statistically significant" in note)
    print(f"PASS: real live-data ANOVA over {len(result_data)} states: {note}")


if __name__ == "__main__":
    test_clearly_significant_difference()
    test_no_significant_difference()
    test_missing_stddev_or_count_never_fabricates_a_result()
    test_single_group_and_empty_result()
    test_wired_into_analyst_judgment_disclosure()
    test_against_real_live_data()
    print("\nAll significance_testing tests passed.")
