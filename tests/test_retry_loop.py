"""
Direct proof that the execute_sql retry loop works, without depending on an LLM
happening to generate a bad query naturally.

Case 1 (recovery): force a real DB error (bad column name) on a real table,
confirm execute_sql captures it and increments sql_attempts, confirm
route_after_execute_sql says "generate_sql", then actually call generate_sql on
the resulting state (which sees the real error) and confirm the corrected query
it produces actually executes successfully.

Case 2 (cap): start at sql_attempts=4 with a query against a table that does not
exist at all (guaranteed to keep failing no matter what), confirm this 5th
attempt sets final_answer with the real error and confirm route_after_execute_sql
says "represent_final_answer" instead of retrying again.
"""

from agents.sql_analyst import (
    add_context,
    execute_sql,
    generate_sql,
    route_after_execute_sql,
)
from models.schema import SQLAnalystState

print("=" * 70)
print("CASE 1: recovery — bad column name on a real table")
print("=" * 70)

# Reuse real, live schema context from add_context so generate_sql has something
# real to work with when it retries.
base_state = SQLAnalystState(
    curated_question="How many orders does each customer state have?",
)
ctx_result = add_context(base_state)
base_state = base_state.model_copy(update=ctx_result)

# Guaranteed-to-fail query: references a column that does not exist.
bad_state = base_state.model_copy(
    update={
        "generated_sql_query": (
            "SELECT customer_state, this_column_does_not_exist "
            "FROM olist_customers_dataset;"
        ),
        "sql_attempts": 0,
    }
)

result1 = execute_sql(bad_state)
print("\nexecute_sql result:")
print("  sql_query_execution_result:", result1["sql_query_execution_result"])
print("  sql_attempts:", result1["sql_attempts"])
print("  final_answer set?:", bool(result1.get("final_answer")))

assert result1["sql_attempts"] == 1, f"expected sql_attempts=1, got {result1['sql_attempts']}"
assert "SQL_EXECUTION_ERROR" in result1["sql_query_execution_result"], (
    "expected an error result, got a clean result — this query should have failed"
)
assert not result1.get("final_answer"), "final_answer should NOT be set yet (only 1 attempt)"

state_after_fail = bad_state.model_copy(update=result1)
route1 = route_after_execute_sql(state_after_fail)
print("  route_after_execute_sql ->", route1)
assert route1 == "generate_sql", f"expected 'generate_sql', got {route1!r}"

# Now actually call generate_sql on this state — it will see the real error
# message via state.sql_query_execution_result and should produce a corrected query.
print("\nCalling generate_sql on the failed state (it sees the real error)...")
regen_result = generate_sql(state_after_fail)
corrected_query = regen_result["generated_sql_query"]
print("  corrected_sql_query:", corrected_query)

# Prove the corrected query is actually valid by executing it for real.
state_with_corrected = state_after_fail.model_copy(update=regen_result)
verify_result = execute_sql(state_with_corrected)
print("\nExecuting the corrected query to verify it actually works...")
print("  sql_query_execution_result:", verify_result["sql_query_execution_result"][:300])
print("  sql_attempts:", verify_result["sql_attempts"])

assert "SQL_EXECUTION_ERROR" not in verify_result["sql_query_execution_result"], (
    "the regenerated query still fails — retry-with-error-context did not work:\n"
    f"{verify_result['sql_query_execution_result']}"
)
print("\nCASE 1 PASSED: retry loop recovered from a real DB error correctly.")


print("\n" + "=" * 70)
print("CASE 2: cap — 5th attempt against a table that does not exist at all")
print("=" * 70)

capped_state = SQLAnalystState(
    curated_question="This will never succeed no matter how many times it retries.",
    generated_sql_query="SELECT * FROM table_that_absolutely_does_not_exist_xyz;",
    sql_attempts=4,
)

result2 = execute_sql(capped_state)
print("\nexecute_sql result (5th attempt):")
print("  sql_query_execution_result:", result2["sql_query_execution_result"])
print("  sql_attempts:", result2["sql_attempts"])
print("  final_answer:", result2.get("final_answer"))

assert result2["sql_attempts"] == 5, f"expected sql_attempts=5, got {result2['sql_attempts']}"
assert "SQL_EXECUTION_ERROR" in result2["sql_query_execution_result"]
final_answer = result2.get("final_answer", "")
assert final_answer, "expected final_answer to be set on the capped attempt, it was empty"
assert "5 attempts" in final_answer, f"expected the give-up message to mention 5 attempts: {final_answer}"
assert "does not exist" in final_answer or "UndefinedTable" in final_answer, (
    f"expected the real DB error to be included in final_answer: {final_answer}"
)

state_after_cap = capped_state.model_copy(update=result2)
route2 = route_after_execute_sql(state_after_cap)
print("  route_after_execute_sql ->", route2)
assert route2 == "represent_final_answer", f"expected 'represent_final_answer', got {route2!r}"

print("\nCASE 2 PASSED: cap correctly stops retrying and writes the give-up final_answer.")

print("\n" + "=" * 70)
print("ALL RETRY LOOP ASSERTIONS PASSED")
print("=" * 70)
