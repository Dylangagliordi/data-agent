"""Standalone test for the represent_final_answer node — invokes just this node."""

from agents.sql_analyst import represent_final_answer
from models.schema import SQLAnalystState

if __name__ == "__main__":
    # Case 1: real success result
    success_state = SQLAnalystState(
        user_question="How many total orders have been recorded in the database?",
        sql_query_execution_result="[{'n': 99441}]",
    )
    result1 = represent_final_answer(success_state)
    print("--- success case ---")
    print("final_answer:", result1["final_answer"])
    print("messages:", result1["messages"])

    # Case 2: already-failed state (execute_sql gave up after 5 attempts) — should
    # pass the existing final_answer straight through, no LLM call, no overwrite.
    failed_state = SQLAnalystState(
        user_question="How many total orders have been recorded in the database?",
        final_answer="The query could not be completed after 5 attempts. Last error: X",
    )
    result2 = represent_final_answer(failed_state)
    print("\n--- already-failed case ---")
    print("final_answer:", result2["final_answer"])
    assert result2["final_answer"] == failed_state.final_answer
    print("assertions passed")
