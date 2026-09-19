"""
CLI entry point for the data agent: the router-orchestrated graph automatically
dispatches an incoming question to either the SQL analyst or the ETL analyst
sub-agent, instead of either one being invoked by hand.

Usage:
    python main.py "how many orders came from São Paulo"
    python main.py "download this file: https://example.com/data.csv into data/mydata as csv"

Prints only the final plain-English answer — not the full graph state.

Every run also appends one JSON line to logs/query_log.jsonl with the run's
trace, branching on which sub-agent actually handled the request:
- route_response == "sql_analyst": the full existing set of fields (as before
  the router existed) plus route_response/route_comments.
- route_response == "etl_analyst": user_question, final_answer,
  route_response, route_comments only — the SQL-specific fields don't apply
  to an ETL run and are omitted entirely, not written as null/empty
  placeholders.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.messages import HumanMessage

from agents.data_agent import build_data_agent_graph
from models.router_schema import DataAgentSchema

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_PATH = PROJECT_ROOT / "logs" / "query_log.jsonl"


def log_run(user_question: str, result: dict) -> None:
    """Append one JSON line capturing this run's trace to logs/query_log.jsonl,
    shaped according to which sub-agent actually handled the request.
    """
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    route_response = result.get("route_response", "")

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "route_response": route_response,
        "route_comments": result.get("route_comments", ""),
    }

    if route_response == "sql_analyst":
        # The SQL analyst's own internal trace (curated_question,
        # generated_sql_query, is_safe, comments, sql_query_execution_result)
        # isn't part of DataAgentSchema's normal fields — sql_node returns the
        # real sub-agent result dict as sql_analyst_trace on the graph's own
        # state for exactly this use (threaded through normally, not a shared
        # module-level global that a concurrent request could overwrite).
        sql_state = result.get("sql_analyst_trace", {})
        entry.update(
            {
                "user_question": user_question,
                "curated_question": sql_state.get("curated_question", ""),
                "generated_sql_query": sql_state.get("generated_sql_query", ""),
                "is_safe": sql_state.get("is_safe", ""),
                "comments": sql_state.get("comments", ""),
                "sql_query_execution_result": sql_state.get("sql_query_execution_result", ""),
                "final_answer": result.get("final_answer", ""),
                "transformation_narrative_log": sql_state.get("transformation_narrative_log", []),
                "transformation_candidates_not_relevant": sql_state.get(
                    "transformation_candidates_not_relevant", []
                ),
            }
        )
    elif route_response == "visualize":
        # visualize_node also returns its SQL analyst sub-agent result as
        # sql_analyst_trace (same field, same pattern as sql_node) so all
        # visualization-specific fields are accessible here.
        sql_state = result.get("sql_analyst_trace", {})
        entry.update(
            {
                "user_question": user_question,
                "curated_question": sql_state.get("curated_question", ""),
                "chart_type": sql_state.get("chart_type", ""),
                "chart_type_source": sql_state.get("chart_type_source", ""),
                "chart_type_reasoning": sql_state.get("chart_type_reasoning", ""),
                "generated_sql_query": sql_state.get("generated_sql_query", ""),
                "is_safe": sql_state.get("is_safe", ""),
                "sql_query_execution_result": sql_state.get("sql_query_execution_result", ""),
                "output_file_path": sql_state.get("output_file_path", ""),
                "chart_image_path": sql_state.get("chart_image_path", ""),
                "final_answer": result.get("final_answer", ""),
                "transformation_narrative_log": sql_state.get("transformation_narrative_log", []),
                "transformation_candidates_not_relevant": sql_state.get(
                    "transformation_candidates_not_relevant", []
                ),
            }
        )
    elif route_response == "etl_analyst":
        entry.update(
            {
                "user_question": user_question,
                "final_answer": result.get("final_answer", ""),
            }
        )
    else:
        # Defensive: route_response should always be one of the two literals
        # once router_node has run, but never silently drop the fact that
        # something unexpected happened here.
        entry.update(
            {
                "user_question": user_question,
                "final_answer": result.get("final_answer", ""),
            }
        )

    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _run_question(question: str) -> dict:
    """Invoke the data-agent graph for question, log the run, and return the result dict."""
    graph = build_data_agent_graph()
    result = graph.invoke(
        DataAgentSchema(messages=[HumanMessage(content=question)]),
        config={"recursion_limit": 50},
    )
    log_run(question, result)
    return result


def main() -> None:
    if len(sys.argv) != 2:
        print(
            'Usage: python main.py "<your question>"\n'
            'Example: python main.py "how many orders came from São Paulo"',
            file=sys.stderr,
        )
        sys.exit(1)

    raw = sys.argv[1]

    # report last — no re-run, pull the most recent query_log entry and build a report.
    if raw.strip().lower() == "report last":
        from utils.generate_report import generate_report, last_query_log_entry
        entry = last_query_log_entry()
        if entry is None:
            print("No entries found in logs/query_log.jsonl — run a query first.", file=sys.stderr)
            sys.exit(1)
        report_path = generate_report(entry)
        print(f"Report: {report_path}")
        print(f'Open with: open "{report_path}"')
        return

    # report: <question> — run the question fresh, then build a report from that run.
    if raw.startswith("report: "):
        from utils.generate_report import generate_report, last_query_log_entry
        question = raw[len("report: "):].strip()
        result = _run_question(question)
        print(result["final_answer"])
        entry = last_query_log_entry()
        if entry is not None:
            report_path = generate_report(entry)
            print(f"\nReport: {report_path}")
            print(f'Open with: open "{report_path}"')
        return

    # present last — no re-run, pull the most recent query_log entry and build a slideshow.
    if raw.strip().lower() == "present last":
        from utils.generate_presentation import generate_presentation
        from utils.generate_report import last_query_log_entry
        entry = last_query_log_entry()
        if entry is None:
            print("No entries found in logs/query_log.jsonl — run a query first.", file=sys.stderr)
            sys.exit(1)
        pres_path = generate_presentation(entry)
        print(f"Presentation: {pres_path}")
        print(f'Open with: open "{pres_path}"')
        return

    # present: <question> — run the question fresh, then build a slideshow from that run.
    if raw.startswith("present: "):
        from utils.generate_presentation import generate_presentation
        from utils.generate_report import last_query_log_entry
        question = raw[len("present: "):].strip()
        result = _run_question(question)
        print(result["final_answer"])
        entry = last_query_log_entry()
        if entry is not None:
            pres_path = generate_presentation(entry)
            print(f"\nPresentation: {pres_path}")
            print(f'Open with: open "{pres_path}"')
        return

    # map — regenerate all three graph PNGs from the real, currently-compiled
    # graphs (Spec 1: Live System Self-Map), never from a stale cached image.
    if raw.strip().lower() == "map":
        from utils.system_map import generate_all_system_maps
        written = generate_all_system_maps(str(PROJECT_ROOT))
        for graph_name, path in written.items():
            print(f"{graph_name}: {path}")
        return

    # dictionary: <table_name> — build a data dictionary for one table
    # (Spec 2), entirely from metadata this project already computes.
    if raw.startswith("dictionary: "):
        from utils.data_dictionary import render_data_dictionary_html
        table_name = raw[len("dictionary: "):].strip()
        path = render_data_dictionary_html(table_name)
        print(f"Data dictionary: {path}")
        print(f'Open with: open "{path}"')
        return

    # Normal question — run through the graph and print the answer.
    result = _run_question(raw)
    print(result["final_answer"])


if __name__ == "__main__":
    main()
