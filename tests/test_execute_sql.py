"""Standalone test for the execute_sql node and its routing function."""

from agents.sql_analyst import execute_sql, route_after_execute_sql
from models.schema import SQLAnalystState

if __name__ == "__main__":
    # 1. Successful query
    ok_state = SQLAnalystState(
        generated_sql_query="SELECT count(*) AS n FROM olist_orders_dataset;"
    )
    ok_result = execute_sql(ok_state)
    print("--- success case ---")
    print("result:", ok_result["sql_query_execution_result"])
    print("attempts:", ok_result["sql_attempts"])
    ok_state_after = ok_state.model_copy(update=ok_result)
    print("route:", route_after_execute_sql(ok_state_after))
    assert route_after_execute_sql(ok_state_after) == "represent_final_answer"

    # 2. Failing query, under the attempt cap -> should route back to generate_sql
    bad_state = SQLAnalystState(
        generated_sql_query="SELECT * FROM this_table_does_not_exist;",
        sql_attempts=0,
    )
    bad_result = execute_sql(bad_state)
    print("\n--- failure case (attempt 1 of 5) ---")
    print("result:", bad_result["sql_query_execution_result"])
    print("attempts:", bad_result["sql_attempts"])
    print("final_answer set?:", bool(bad_result.get("final_answer")))
    bad_state_after = bad_state.model_copy(update=bad_result)
    print("route:", route_after_execute_sql(bad_state_after))
    assert route_after_execute_sql(bad_state_after) == "generate_sql"

    # 3. Failing query, already at attempt 4 -> this becomes attempt 5, should stop
    capped_state = SQLAnalystState(
        generated_sql_query="SELECT * FROM this_table_does_not_exist;",
        sql_attempts=4,
    )
    capped_result = execute_sql(capped_state)
    print("\n--- failure case (attempt 5 of 5, cap reached) ---")
    print("result:", capped_result["sql_query_execution_result"])
    print("attempts:", capped_result["sql_attempts"])
    print("final_answer:", capped_result.get("final_answer"))
    capped_state_after = capped_state.model_copy(update=capped_result)
    print("route:", route_after_execute_sql(capped_state_after))
    assert route_after_execute_sql(capped_state_after) == "represent_final_answer"
    assert capped_result["sql_attempts"] == 5

    print("\nall assertions passed")
