"""Standalone test for the is_safe node — invokes just this node.

Tests both a safe read-only query and an unsafe write query to confirm the
judge actually discriminates (not just always saying yes).
"""

from agents.sql_analyst import is_safe
from models.schema import SQLAnalystState

if __name__ == "__main__":
    safe_state = SQLAnalystState(
        generated_sql_query=(
            "SELECT c.customer_state, AVG(op.payment_value) "
            "FROM olist_order_payments_dataset op "
            "JOIN olist_orders_dataset o ON o.order_id = op.order_id "
            "JOIN olist_customers_dataset c ON c.customer_id = o.customer_id "
            "GROUP BY c.customer_state;"
        )
    )
    safe_result = is_safe(safe_state)
    print("SAFE query judged:", safe_result["is_safe"], "| comments:", safe_result["comments"])

    unsafe_state = SQLAnalystState(
        generated_sql_query="DELETE FROM olist_orders_dataset WHERE order_id = 'abc123';"
    )
    unsafe_result = is_safe(unsafe_state)
    print("UNSAFE query judged:", unsafe_result["is_safe"], "| comments:", unsafe_result["comments"])
