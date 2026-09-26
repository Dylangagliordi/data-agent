"""
CLI entry point for the data agent: the router-orchestrated graph automatically
dispatches an incoming question to either the SQL analyst or the ETL analyst
sub-agent, instead of either one being invoked by hand.

Usage:
    python main.py "how many orders came from São Paulo"
    python main.py "download this file: https://example.com/data.csv into data/mydata as csv"

Prints only the final plain-English answer — not the full graph state.

Every run also appends one JSON line to logs/query_log.jsonl with the run's
trace — see utils/cli_modes.py:log_run for the exact shape.

Spec 15, Part 1 (Mode-Infrastructure Formalization): the 22 special-case
commands (report last, map, profile:, prepare:, ...) used to be 22 hand-
written `if` branches here. They're now real, structured entries in
utils.cli_modes.MODES — this file just tries that registry first, and falls
through to a plain question when nothing matches. Adding a 23rd mode means
adding one entry to that registry, never touching this file.
"""

import sys

from utils.cli_modes import dispatch, run_question


def main() -> None:
    if len(sys.argv) != 2:
        print(
            'Usage: python main.py "<your question>"\n'
            'Example: python main.py "how many orders came from São Paulo"',
            file=sys.stderr,
        )
        sys.exit(1)

    raw = sys.argv[1]

    if dispatch(raw):
        return

    # Normal question — run through the graph and print the answer.
    result = run_question(raw)
    print(result["final_answer"])


if __name__ == "__main__":
    main()
