"""Standalone test for the add_context node — invokes just this node.

Also verifies the deterministic fan-out detection (added alongside the general
fan-out principle in generate_sql's prompt): the resulting context string must contain
an explicit WARNING for a table known to have multiple rows per a foreign key
(olist_order_payments_dataset has multiple rows per order_id whenever a customer splits
payment across methods), and must NOT flag a table that has no such issue (false-positive
check) — olist_sellers_dataset's only id-like column is seller_id, which is that table's
own primary key (one row per seller), so it should never be flagged as fan-out.
"""

from agents.sql_analyst import add_context
from models.schema import SQLAnalystState

if __name__ == "__main__":
    state = SQLAnalystState(user_question="how many orders are there?")
    result = add_context(state)
    ctx = result["prompt_query_context"]
    print("context length (chars):", len(ctx))
    print("--- first 2000 chars ---")
    print(ctx[:2000])

    print("\n--- fan-out warning checks ---")
    known_fanout = "WARNING: olist_order_payments_dataset has multiple rows per order_id"
    assert known_fanout in ctx, f"expected fan-out warning missing: {known_fanout!r}"
    print(f"PASS: found expected warning for olist_order_payments_dataset / order_id")

    false_positive = "olist_sellers_dataset has multiple rows per"
    assert false_positive not in ctx, f"unexpected fan-out warning present: {false_positive!r}"
    print("PASS: no false-positive warning for olist_sellers_dataset (single row per seller_id)")
