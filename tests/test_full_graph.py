"""
Full end-to-end test of the compiled SQL analyst graph, invoked with one real
question that requires joining at least two tables (order items + products,
to find the average price per product category).
"""

from agents.sql_analyst import build_sql_analyst_graph
from models.schema import SQLAnalystState

if __name__ == "__main__":
    graph = build_sql_analyst_graph()

    question = (
        "What are the top 5 product categories by total sales revenue, "
        "joining order items with products?"
    )
    initial_state = SQLAnalystState(user_question=question)

    final_state = graph.invoke(initial_state, config={"recursion_limit": 50})

    print("=" * 70)
    print("USER QUESTION:", question)
    print("=" * 70)
    print("\nCURATED QUESTION:\n", final_state["curated_question"])
    print("\nGENERATED SQL:\n", final_state["generated_sql_query"])
    print("\nIS_SAFE:", final_state["is_safe"], "| COMMENTS:", final_state["comments"])
    print("\nSQL ATTEMPTS:", final_state["sql_attempts"])
    print("\nRAW EXECUTION RESULT:\n", final_state["sql_query_execution_result"])
    print("\nFINAL ANSWER:\n", final_state["final_answer"])
    print("\n--- messages trace ---")
    for m in final_state["messages"]:
        print(f"[{type(m).__name__}] {m.content[:200]}")
