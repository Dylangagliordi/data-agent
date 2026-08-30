"""Standalone test for the generate_sql node — invokes just this node.

Reuses add_context to get a real live schema string, then asks generate_sql
for a query that requires joining at least two tables.
"""

from agents.sql_analyst import add_context, generate_sql
from models.schema import SQLAnalystState

if __name__ == "__main__":
    state = SQLAnalystState(
        curated_question=(
            "What is the average payment value per order, joined with the customer's state?"
        )
    )
    ctx_result = add_context(state)
    state = state.model_copy(update=ctx_result)

    result = generate_sql(state)
    print("--- generated_sql_query ---")
    print(result["generated_sql_query"])
