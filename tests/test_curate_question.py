"""Standalone test for the curate_question node — invokes just this node."""

from agents.sql_analyst import curate_question
from models.schema import SQLAnalystState

if __name__ == "__main__":
    state = SQLAnalystState(user_question="how many orders we got total in the db like ever")
    result = curate_question(state)
    print("curated_question:", repr(result["curated_question"]))
    print("messages:", result["messages"])
