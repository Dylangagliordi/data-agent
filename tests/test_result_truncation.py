"""
Direct proof of the fix for the "unbounded result -> fabricated summary" bug.

Reproduces the real failure: a LIMIT-less query joining customers/orders/
payments that matches far more than MAX_RESULT_ROWS rows (confirmed live:
~96,096 distinct customers), which previously produced a ~10MB result string
that the summarizer silently truncated internally and fabricated invented
statistics over (logged real example: claimed "Total Records: 50 customers"
with fake min/max/average that didn't correspond to the actual data at all).

This test proves:
1. execute_sql caps rows at MAX_RESULT_ROWS and marks the result as truncated.
2. represent_final_answer, given a truncated result, deterministically reports
   the limitation instead of asking an LLM to summarize it — a live low-tier
   model was observed fabricating aggregate stats over a truncated sample even
   with an explicit prompt instruction not to, so this must not depend on LLM
   compliance at all.
"""

from agents.sql_analyst import MAX_RESULT_ROWS, execute_sql, represent_final_answer
from models.schema import SQLAnalystState

if __name__ == "__main__":
    print("=" * 70)
    print("STEP 1: execute_sql caps an unbounded, large result set")
    print("=" * 70)

    unbounded_state = SQLAnalystState(
        generated_sql_query=(
            "SELECT c.customer_unique_id, SUM(p.payment_value) AS total_spent "
            "FROM olist_customers_dataset c "
            "JOIN olist_orders_dataset o ON o.customer_id = c.customer_id "
            "JOIN olist_order_payments_dataset p ON p.order_id = o.order_id "
            "GROUP BY c.customer_unique_id "
            "ORDER BY total_spent DESC;"  # deliberately no LIMIT
        )
    )
    exec_result = execute_sql(unbounded_state)
    raw_result = exec_result["sql_query_execution_result"]

    import json as _json
    print("result length (chars):", len(raw_result))
    parsed = _json.loads(raw_result)
    print("truncated field:", parsed.get("truncated"))
    print("row count in payload:", len(parsed.get("rows", [])))

    assert parsed.get("truncated") is True, (
        "expected the result to be marked truncated — this query is known to match "
        "~96,096 rows, far more than MAX_RESULT_ROWS"
    )
    assert len(parsed["rows"]) == MAX_RESULT_ROWS, (
        f"expected exactly {MAX_RESULT_ROWS} rows in payload, got {len(parsed['rows'])}"
    )
    # Sanity: the capped result should be small (KB), not the ~10MB blob seen before the fix.
    assert len(raw_result) < 100_000, f"result is still huge ({len(raw_result)} chars) — cap did not work"
    print(f"\nPASSED: result capped and marked truncated (MAX_RESULT_ROWS={MAX_RESULT_ROWS}).")

    print("\n" + "=" * 70)
    print("STEP 2: represent_final_answer deterministically reports the limitation")
    print("(no LLM call at all for a truncated result — cannot fabricate stats)")
    print("=" * 70)

    state_with_result = unbounded_state.model_copy(update=exec_result)
    state_with_result = state_with_result.model_copy(
        update={"user_question": "Show me which customers spent the most, and update their loyalty tier to Gold."}
    )

    summary_result = represent_final_answer(state_with_result)
    answer = summary_result["final_answer"]
    print("\nfinal_answer:")
    print(answer)

    assert "capped at" in answer.lower() or "could not" in answer.lower() or "can't" in answer.lower(), (
        f"expected an honest limitation message, got: {answer}"
    )
    # None of these fabricated-looking claims (from the real logged failure) should appear.
    forbidden = ["total records", "average spending", "highest spent", "lowest spent"]
    lowered = answer.lower()
    bad_hits = [p for p in forbidden if p in lowered]
    assert not bad_hits, f"answer still contains fabricated-looking aggregate claims: {bad_hits}"

    print("\nPASSED: no fabricated aggregate claims — limitation reported honestly and deterministically.")
    print("\n" + "=" * 70)
    print("ALL ASSERTIONS PASSED")
    print("=" * 70)

