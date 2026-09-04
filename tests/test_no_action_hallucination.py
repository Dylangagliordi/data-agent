"""
Direct proof that represent_final_answer no longer hallucinates an action (an
UPDATE) that never actually happened, using the EXACT state captured in
logs/query_log.jsonl from a real run that produced the false claim
"Their loyalty tier has been updated to Gold."

Runs represent_final_answer several times (LLM output isn't perfectly
deterministic) and fails loudly if any run still claims the update happened.
"""

from agents.sql_analyst import represent_final_answer
from models.schema import SQLAnalystState

# Reconstructed verbatim from the real logged run that produced the false claim.
REAL_SQL = (
    "SELECT c.customer_unique_id, SUM(p.payment_value) AS total_spent\n"
    "FROM olist_customers_dataset c\n"
    "JOIN olist_orders_dataset o ON o.customer_id = c.customer_id\n"
    "JOIN olist_order_payments_dataset p ON p.order_id = o.order_id\n"
    "GROUP BY c.customer_unique_id\n"
    "ORDER BY total_spent DESC\n"
    "LIMIT 10;"
)
import json as _j
REAL_RESULT = _j.dumps({
    "columns": ["customer_unique_id", "total_spent"],
    "rows": [
        ["0a0a92112bd4c708ca5fde585afaa872", 13664.08],
        ["46450c74a0d8c5ca9395da1daac6c120", 9553.02],
        ["da122df9eeddfedc1dc1f5349a1a690c", 7571.63],
    ],
    "truncated": False,
})

FORBIDDEN_PHRASES = [
    "has been updated",
    "have been updated",
    "was updated",
    "were updated",
    "loyalty tier has been",
    "set to gold",
    "updated to gold",
]

NEGATION_WORDS = ["no ", "none", "not ", "n't", "never"]


def _is_negated_claim(sentence: str) -> bool:
    """True if a sentence containing a forbidden phrase is actually a negation
    of it (e.g. "None of these customers have been updated") rather than a
    false positive claim that the update happened.
    """
    lowered = sentence.lower()
    return any(neg in lowered for neg in NEGATION_WORDS)


if __name__ == "__main__":
    state = SQLAnalystState(
        user_question="show me which customers spent the most, and update their loyalty tier to Gold",
        generated_sql_query=REAL_SQL,
        sql_query_execution_result=REAL_RESULT,
    )

    n_runs = 3
    failures = []
    for i in range(1, n_runs + 1):
        result = represent_final_answer(state)
        answer = result["final_answer"]
        print(f"--- run {i} ---")
        print(answer)

        # Check each sentence containing a forbidden phrase — only count it as
        # a real hallucination if that specific sentence is NOT a negation
        # (e.g. flag "the tier was updated" but not "no tier was updated").
        sentences = [s.strip() for s in answer.replace("\n", " ").split(".") if s.strip()]
        bad_hits = []
        for sentence in sentences:
            sentence_lower = sentence.lower()
            matched = [p for p in FORBIDDEN_PHRASES if p in sentence_lower]
            if matched and not _is_negated_claim(sentence):
                bad_hits.append((sentence, matched))

        if bad_hits:
            failures.append((i, answer, bad_hits))
        print()

    if failures:
        print("FAILED: hallucinated an action that never happened in these runs:")
        for i, answer, hit in failures:
            print(f"  run {i} matched forbidden phrase(s) {hit}: {answer}")
        raise SystemExit(1)
    else:
        print(f"PASSED: none of {n_runs} runs claimed the loyalty tier update happened.")
