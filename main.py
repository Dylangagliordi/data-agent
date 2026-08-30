"""
CLI entry point for the SQL analyst sub-agent.

Usage:
    python main.py "how many orders came from São Paulo"

Prints only the final plain-English answer — not the full graph state.

Every run also appends one JSON line to logs/query_log.jsonl with the full
intermediate trace (curated question, generated SQL, safety verdict, raw
execution result, final answer) so a run can be inspected after the fact —
this is purely an additional trace file and does not change stdout.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from agents.sql_analyst import build_sql_analyst_graph
from models.schema import SQLAnalystState

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_PATH = PROJECT_ROOT / "logs" / "query_log.jsonl"


def log_run(final_state: dict) -> None:
    """Append one JSON line capturing this run's full trace to logs/query_log.jsonl."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_question": final_state.get("user_question", ""),
        "curated_question": final_state.get("curated_question", ""),
        "generated_sql_query": final_state.get("generated_sql_query", ""),
        "is_safe": final_state.get("is_safe", ""),
        "comments": final_state.get("comments", ""),
        "sql_query_execution_result": final_state.get("sql_query_execution_result", ""),
        "final_answer": final_state.get("final_answer", ""),
    }
    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def main() -> None:
    if len(sys.argv) != 2:
        print(
            'Usage: python main.py "<your question>"\n'
            'Example: python main.py "how many orders came from São Paulo"',
            file=sys.stderr,
        )
        sys.exit(1)

    question = sys.argv[1]
    graph = build_sql_analyst_graph()
    final_state = graph.invoke(
        SQLAnalystState(user_question=question),
        config={"recursion_limit": 50},
    )
    log_run(final_state)
    print(final_state["final_answer"])


if __name__ == "__main__":
    main()
