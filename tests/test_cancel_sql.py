"""Standalone test for the cancel_sql node — invokes just this node."""

from agents.sql_analyst import cancel_sql
from models.schema import SQLAnalystState

if __name__ == "__main__":
    state = SQLAnalystState(
        generated_sql_query="DELETE FROM olist_orders_dataset WHERE order_id = 'abc123';",
        is_safe="no",
        comments="The query contains the DELETE keyword, which modifies data.",
    )
    result = cancel_sql(state)
    print("final_answer:", result["final_answer"])
    print("messages:", result["messages"])
    assert "DELETE keyword" in result["final_answer"]
    assert len(result["messages"]) == 1
    print("assertions passed")
