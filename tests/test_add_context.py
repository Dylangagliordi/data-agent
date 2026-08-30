"""Standalone test for the add_context node — invokes just this node."""

from agents.sql_analyst import add_context
from models.schema import SQLAnalystState

if __name__ == "__main__":
    state = SQLAnalystState(user_question="how many orders are there?")
    result = add_context(state)
    ctx = result["prompt_query_context"]
    print("context length (chars):", len(ctx))
    print("--- first 2000 chars ---")
    print(ctx[:2000])
