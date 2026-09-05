"""Standalone test for the is_safe node — invokes just this node.

Tests a safe read-only query, an unsafe write query, and — the deterministic AST
gate added for architecture review point #23 — a deliberately malicious
semicolon-chained multi-statement injection, which must be rejected OUTRIGHT by
_ast_safety_check before is_safe's LLM call is even reached (the LLM judge here
was foolable by comment tricks, encoding, or exactly this kind of injection, since
psycopg2 happily executes multiple ';'-separated statements in one call).
"""

from agents.sql_analyst import _ast_safety_check, is_safe
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
    assert safe_result["is_safe"] == "yes", "expected a plain read-only SELECT to be judged safe"

    unsafe_state = SQLAnalystState(
        generated_sql_query="DELETE FROM olist_orders_dataset WHERE order_id = 'abc123';"
    )
    unsafe_result = is_safe(unsafe_state)
    print("UNSAFE query judged:", unsafe_result["is_safe"], "| comments:", unsafe_result["comments"])
    assert unsafe_result["is_safe"] == "no", "expected a bare DELETE to be judged unsafe"

    # ── Deterministic AST gate: multi-statement injection ──────────────────────
    print("\n" + "=" * 70)
    print("AST SAFETY GATE: semicolon-chained multi-statement injection")
    print("=" * 70)

    injection_sql = (
        "SELECT * FROM olist_orders_dataset; "
        "DROP TABLE olist_orders_dataset;"
    )

    # Direct check on the deterministic gate itself, with no LLM call involved.
    ast_ok, ast_reason = _ast_safety_check(injection_sql)
    print("AST check result:", ast_ok, "| reason:", ast_reason)
    assert ast_ok is False, "expected the multi-statement injection to be rejected by the AST check"
    assert "one sql statement" in ast_reason.lower() or "statement" in ast_reason.lower()

    # Through the actual is_safe node: must be rejected WITHOUT ever calling the LLM.
    # Monkeypatch pick_llm to blow up if it's ever invoked from this call, proving the
    # AST gate short-circuits before the (now-secondary, non-authoritative) LLM check.
    import agents.sql_analyst as sql_analyst_module

    def _llm_should_not_be_called(level):
        raise AssertionError(
            "pick_llm() was called — the AST gate should have rejected this query "
            "before the LLM judge was ever consulted"
        )

    original_pick_llm = sql_analyst_module.pick_llm
    sql_analyst_module.pick_llm = _llm_should_not_be_called
    try:
        injection_state = SQLAnalystState(generated_sql_query=injection_sql)
        injection_result = is_safe(injection_state)
    finally:
        sql_analyst_module.pick_llm = original_pick_llm

    print("Injection judged:", injection_result["is_safe"], "| comments:", injection_result["comments"])
    assert injection_result["is_safe"] == "no", "expected the multi-statement injection to be judged unsafe"
    assert "deterministic" in injection_result["comments"].lower()
    print("PASS: multi-statement injection rejected outright by the AST gate, LLM never consulted.")

    # A single valid SELECT with a trailing semicolon and comment-only "second
    # statement" text must NOT be rejected — the gate must not be overzealous.
    trailing_comment_sql = "SELECT 1 -- ; DROP TABLE foo"
    ok2, reason2 = _ast_safety_check(trailing_comment_sql)
    assert ok2 is True, f"expected a comment-only trailing 'statement' to be accepted, got: {reason2!r}"
    print("PASS: a real second statement inside a SQL comment does not falsely trigger the gate.")

    # SELECT ... INTO (creates a table) must also be rejected.
    select_into_sql = "SELECT * INTO new_table FROM olist_orders_dataset"
    ok3, reason3 = _ast_safety_check(select_into_sql)
    assert ok3 is False, "expected SELECT ... INTO to be rejected (it creates a table)"
    print("PASS: SELECT ... INTO is rejected.", "| reason:", reason3)

    # A CTE (WITH ... SELECT) must still be accepted as read-only.
    cte_sql = "WITH totals AS (SELECT customer_id, COUNT(*) AS n FROM olist_orders_dataset GROUP BY customer_id) SELECT * FROM totals"
    ok4, reason4 = _ast_safety_check(cte_sql)
    assert ok4 is True, f"expected a WITH ... SELECT CTE to be accepted, got: {reason4!r}"
    print("PASS: WITH ... SELECT CTE is correctly accepted as read-only.")

    print("\nALL is_safe / AST-GATE ASSERTIONS PASSED")
