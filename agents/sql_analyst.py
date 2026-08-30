"""
SQL analyst sub-agent: LangGraph node definitions.
"""

from langchain_core.messages import HumanMessage

from models.schema import SQLAnalystState
from utils.llm_pick import pick_llm

CURATE_QUESTION_SYSTEM_PROMPT = """You clean up the wording of a raw user question about a \
database. Fix grammar, spelling, and phrasing only. Do NOT change what the question is \
actually asking, do NOT add new constraints, and do NOT answer it. Output only the cleaned-up \
question text, nothing else."""


def curate_question(state: SQLAnalystState) -> dict:
    """Node 1: clean up the raw question's wording only (no LLM reasoning about intent).

    Appends the curated question to messages as a HumanMessage.
    """
    llm = pick_llm("low")
    response = llm.invoke(
        [
            ("system", CURATE_QUESTION_SYSTEM_PROMPT),
            ("human", state.user_question),
        ]
    )
    curated = response.content.strip()

    return {
        "curated_question": curated,
        "messages": [HumanMessage(content=curated)],
    }
