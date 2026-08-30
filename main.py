"""
CLI entry point for the SQL analyst sub-agent.

Usage:
    python main.py "how many orders came from São Paulo"

Prints only the final plain-English answer — not the full graph state.
"""

import sys

from agents.sql_analyst import build_sql_analyst_graph
from models.schema import SQLAnalystState


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
    print(final_state["final_answer"])


if __name__ == "__main__":
    main()
