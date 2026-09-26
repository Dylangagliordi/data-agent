"""CLI Mode Registry (Spec 15, Part 1: Mode-Infrastructure Formalization).

Before this, main.py's dispatch was 22 hand-written `if` branches stacked up
one per spec, each with its own local imports, its own argument parsing, and
its own prints — copy-pasted from the last one every time a new mode was
added, with no single place listing "here are all the modes." Worse,
utils/doc_drift.py's CLI-command inventory (Spec 13) had to regex-parse
main.py's own source text to reconstruct what should just be structured data.

This is the real, structured replacement: MODES is a list of Mode entries,
each pairing a trigger (raw input -> parsed args, or None if it doesn't
match) with a handler (runs the mode given those parsed args). main.py's
`main()` just iterates MODES, calls the first matching trigger, and calls its
handler — adding a 23rd mode means adding one entry here, never touching that
loop. doc_drift.py reads MODES directly for its inventory instead of
regex-parsing source text.

This is a pure refactor of the DISPATCH MECHANISM only — every mode's real
behavior (imports, prints, error messages, exit codes) is carried over
unchanged from main.py's prior inline branches.

Public interface:
    Mode (dataclass)
    exact_match(command) -> trigger function
    prefixed(prefix, arg_name="arg") -> trigger function
    MODES: list[Mode]
    log_run(user_question, result) -> None
    run_question(question) -> dict
"""

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from langchain_core.messages import HumanMessage

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "logs" / "query_log.jsonl"


def log_run(user_question: str, result: dict) -> None:
    """Append one JSON line capturing this run's trace to logs/query_log.jsonl,
    shaped according to which sub-agent actually handled the request.
    Unchanged from main.py's original implementation — moved here so the
    handlers below (and main.py's fallback branch) share one copy.
    """
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    route_response = result.get("route_response", "")

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "route_response": route_response,
        "route_comments": result.get("route_comments", ""),
    }

    if route_response == "sql_analyst":
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
        entry.update({"user_question": user_question, "final_answer": result.get("final_answer", "")})
    else:
        entry.update({"user_question": user_question, "final_answer": result.get("final_answer", "")})

    with open(LOG_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run_question(question: str) -> dict:
    """Invoke the data-agent graph for question, log the run, and return the
    result dict. Unchanged from main.py's original _run_question."""
    from agents.data_agent import build_data_agent_graph
    from models.router_schema import DataAgentSchema

    graph = build_data_agent_graph()
    result = graph.invoke(
        DataAgentSchema(messages=[HumanMessage(content=question)]),
        config={"recursion_limit": 50},
    )
    log_run(question, result)
    return result


@dataclass
class Mode:
    """One CLI command: name (for display/inventory), trigger (raw input ->
    parsed args dict, or None if this mode doesn't match), handler (runs the
    mode given those parsed args)."""

    name: str
    trigger: Callable[[str], "Optional[dict]"]
    handler: Callable[[dict], None]


def exact_match(command: str) -> Callable[[str], "Optional[dict]"]:
    """Trigger builder for the `raw.strip().lower() == "..."` shape — 12 of
    the 22 real commands. Matches with no arguments (an empty dict)."""

    def _trigger(raw: str) -> "Optional[dict]":
        return {} if raw.strip().lower() == command else None

    return _trigger


def prefixed(prefix: str, arg_name: str = "arg") -> Callable[[str], "Optional[dict]"]:
    """Trigger builder for the `raw.startswith("...")` + slice shape — 9 of
    the 22 real commands. Matches with {arg_name: <real, stripped remainder>}."""

    def _trigger(raw: str) -> "Optional[dict]":
        if raw.startswith(prefix):
            return {arg_name: raw[len(prefix):].strip()}
        return None

    return _trigger


def _prepare_trigger(raw: str) -> "Optional[dict]":
    """The one genuinely different shape: "prepare: <table> for <goal>" needs
    a second split on " for " beyond a simple prefix strip — its own function
    rather than a forced generalization of prefixed()."""
    prefix = "prepare: "
    if not raw.startswith(prefix) or " for " not in raw:
        return None
    body = raw[len(prefix):]
    table_name, goal = body.split(" for ", 1)
    return {"table_name": table_name.strip(), "goal": goal.strip()}


# ── Handlers — one per mode, behavior carried over unchanged from main.py's
# original inline branches. Each does its own local imports, matching the
# established convention of keeping heavy/optional dependencies out of this
# module's own import-time cost. ────────────────────────────────────────────


def _handle_report_last(args: dict) -> None:
    from utils.generate_report import generate_report, last_query_log_entry

    entry = last_query_log_entry()
    if entry is None:
        print("No entries found in logs/query_log.jsonl — run a query first.", file=sys.stderr)
        sys.exit(1)
    report_path = generate_report(entry)
    print(f"Report: {report_path}")
    print(f'Open with: open "{report_path}"')


def _handle_report_question(args: dict) -> None:
    from utils.generate_report import generate_report, last_query_log_entry

    result = run_question(args["arg"])
    print(result["final_answer"])
    entry = last_query_log_entry()
    if entry is not None:
        report_path = generate_report(entry)
        print(f"\nReport: {report_path}")
        print(f'Open with: open "{report_path}"')


def _handle_present_last(args: dict) -> None:
    from utils.generate_presentation import generate_presentation
    from utils.generate_report import last_query_log_entry

    entry = last_query_log_entry()
    if entry is None:
        print("No entries found in logs/query_log.jsonl — run a query first.", file=sys.stderr)
        sys.exit(1)
    pres_path = generate_presentation(entry)
    print(f"Presentation: {pres_path}")
    print(f'Open with: open "{pres_path}"')


def _handle_present_question(args: dict) -> None:
    from utils.generate_presentation import generate_presentation
    from utils.generate_report import last_query_log_entry

    result = run_question(args["arg"])
    print(result["final_answer"])
    entry = last_query_log_entry()
    if entry is not None:
        pres_path = generate_presentation(entry)
        print(f"\nPresentation: {pres_path}")
        print(f'Open with: open "{pres_path}"')


def _handle_map(args: dict) -> None:
    from utils.system_map import generate_all_system_maps

    written = generate_all_system_maps(str(PROJECT_ROOT))
    for graph_name, path in written.items():
        print(f"{graph_name}: {path}")


def _handle_dictionary(args: dict) -> None:
    from utils.data_dictionary import render_data_dictionary_html

    path = render_data_dictionary_html(args["arg"])
    print(f"Data dictionary: {path}")
    print(f'Open with: open "{path}"')


def _handle_dq_backlog(args: dict) -> None:
    from utils.dq_backlog import render_dq_backlog_html

    path = render_dq_backlog_html()
    print(f"DQ backlog: {path}")
    print(f'Open with: open "{path}"')


def _handle_compare(args: dict) -> None:
    from utils.run_comparison import render_run_comparison_html

    question = args["arg"]
    path = render_run_comparison_html(question)
    if path is None:
        print(f"Not enough history to compare — need at least 2 past runs of exactly: {question!r}")
    else:
        print(f"Run comparison: {path}")
        print(f'Open with: open "{path}"')


def _handle_freshness(args: dict) -> None:
    from utils.freshness_briefing import render_freshness_briefing_html

    path = render_freshness_briefing_html()
    print(f"Freshness briefing: {path}")
    print(f'Open with: open "{path}"')


def _handle_prepare(args: dict) -> None:
    from utils.transformation_options import plan_transformations_for_goal

    table_name, goal = args["table_name"], args["goal"]
    if not table_name or not goal:
        print("Usage: prepare: <table> for <goal>", file=sys.stderr)
        sys.exit(1)
    result = plan_transformations_for_goal(table_name, goal)
    print(f"Prepared {result['table_name']!r} for: {result['goal']}")
    if result["already_decided"]:
        print(f"  Already decided (reused silently): {len(result['already_decided'])}")
    if result["not_relevant"]:
        print(f"  Not relevant to this goal: {len(result['not_relevant'])}")
    for d in result["decided_this_run"]:
        status = "applied" if d["applied"] else "chosen, not applied (e.g. skipped)"
        print(f"  {d['kind']} ({d['candidate_id']}): {d['chosen_option_id']} — {status}")
    if not result["decided_this_run"] and not result["already_decided"]:
        print("  No stored candidates found for this table.")


def _handle_inventory(args: dict) -> None:
    from utils.doc_drift import render_inventory_html

    path = render_inventory_html()
    print(f"Code inventory: {path}")
    print(f'Open with: open "{path}"')


def _handle_profile(args: dict) -> None:
    from utils.auto_eda import render_auto_eda_html

    path = render_auto_eda_html(args["arg"])
    print(f"Auto-EDA profile: {path}")
    print(f'Open with: open "{path}"')


def _handle_joins(args: dict) -> None:
    from utils.join_advisory import render_join_advisory_html

    path = render_join_advisory_html()
    print(f"Join advisory: {path}")
    print(f'Open with: open "{path}"')


def _handle_sources(args: dict) -> None:
    from utils.ingestion_registry import render_ingestion_sources_html

    path = render_ingestion_sources_html()
    print(f"Ingestion sources: {path}")
    print(f'Open with: open "{path}"')


def _handle_define_metric(args: dict) -> None:
    from utils.load_data import ensure_saved_metrics_table, get_admin_connection, write_saved_metric
    from utils.semantic_layer import parse_define_metric_command

    try:
        parsed = parse_define_metric_command(args["arg"])
    except ValueError as e:
        print(f"Could not define metric: {e}", file=sys.stderr)
        sys.exit(1)
    conn = get_admin_connection()
    try:
        ensure_saved_metrics_table(conn)
        write_saved_metric(conn, parsed["metric_name"], parsed["sql_fragment"], parsed["description"])
    finally:
        conn.close()
    print(f"Defined metric {parsed['metric_name']!r}: {parsed['sql_fragment']}")


def _handle_delete_metric(args: dict) -> None:
    from utils.load_data import delete_saved_metric, ensure_saved_metrics_table, get_admin_connection

    metric_name = args["arg"]
    conn = get_admin_connection()
    try:
        ensure_saved_metrics_table(conn)
        deleted = delete_saved_metric(conn, metric_name)
    finally:
        conn.close()
    if deleted:
        print(f"Deleted metric {metric_name!r}")
    else:
        print(f"No metric named {metric_name!r} was defined")


def _handle_metrics(args: dict) -> None:
    from utils.load_data import ensure_saved_metrics_table, get_admin_connection, read_saved_metrics
    from utils.semantic_layer import render_semantic_layer_html

    conn = get_admin_connection()
    try:
        ensure_saved_metrics_table(conn)
        metrics = read_saved_metrics(conn)
    finally:
        conn.close()
    path = render_semantic_layer_html(metrics)
    print(f"Semantic layer: {path}")
    print(f'Open with: open "{path}"')


def _handle_taxonomy(args: dict) -> None:
    from utils.taxonomy_governance import render_taxonomy_governance_html

    path = render_taxonomy_governance_html()
    print(f"Taxonomy governance: {path}")
    print(f'Open with: open "{path}"')


def _handle_rubric_dashboard(args: dict) -> None:
    from utils.rubric_dashboard import render_rubric_dashboard_html

    path = render_rubric_dashboard_html()
    print(f"Rubric dashboard: {path}")
    print(f'Open with: open "{path}"')


def _handle_explain(args: dict) -> None:
    from utils.generate_report import generate_report
    from utils.run_comparison import find_entries_for_question

    question = args["arg"]
    entries = find_entries_for_question(question)
    if not entries:
        print(
            f"No past run found for exactly: {question!r} — explain: only looks up "
            "history, it never runs a question fresh (use report: for that).",
            file=sys.stderr,
        )
        sys.exit(1)
    report_path = generate_report(entries[-1])
    print(f"Explanation: {report_path}")
    print(f'Open with: open "{report_path}"')


def _handle_audit(args: dict) -> None:
    from utils.audit_export import render_table_audit_html

    path = render_table_audit_html(args["arg"])
    print(f"Audit history: {path}")
    print(f'Open with: open "{path}"')


def _handle_notebook_question(args: dict) -> None:
    from utils.generate_report import last_query_log_entry
    from utils.notebook_export import render_notebook_export

    result = run_question(args["arg"])
    print(result["final_answer"])
    entry = last_query_log_entry()
    if entry is not None:
        path = render_notebook_export(entry)
        print(f"\nNotebook: {path}")


def _handle_notebook_last(args: dict) -> None:
    from utils.generate_report import last_query_log_entry
    from utils.notebook_export import render_notebook_export

    entry = last_query_log_entry()
    if entry is None:
        print("No entries found in logs/query_log.jsonl — run a query first.", file=sys.stderr)
        sys.exit(1)
    path = render_notebook_export(entry)
    print(f"Notebook: {path}")


# Mode.name is deliberately the exact short trigger string (e.g. "profile: ",
# not "profile: <table_name>") — this is what utils/doc_drift.py's inventory
# already reported before this refactor (via regex-parsing main.py's source),
# and what its own test asserts exact membership against. A more descriptive
# per-mode display string would be a real, separate improvement, but changing
# the shape of what's reported here isn't this spec's job — this spec is
# about the dispatch MECHANISM, not the inventory's display format.
MODES: list = [
    Mode("report last", exact_match("report last"), _handle_report_last),
    Mode("report: ", prefixed("report: "), _handle_report_question),
    Mode("present last", exact_match("present last"), _handle_present_last),
    Mode("present: ", prefixed("present: "), _handle_present_question),
    Mode("map", exact_match("map"), _handle_map),
    Mode("dictionary: ", prefixed("dictionary: "), _handle_dictionary),
    Mode("dq backlog", exact_match("dq backlog"), _handle_dq_backlog),
    Mode("compare: ", prefixed("compare: "), _handle_compare),
    Mode("freshness", exact_match("freshness"), _handle_freshness),
    Mode("prepare: ", _prepare_trigger, _handle_prepare),
    Mode("inventory", exact_match("inventory"), _handle_inventory),
    Mode("profile: ", prefixed("profile: "), _handle_profile),
    Mode("joins", exact_match("joins"), _handle_joins),
    Mode("sources", exact_match("sources"), _handle_sources),
    Mode("define metric: ", prefixed("define metric: "), _handle_define_metric),
    Mode("delete metric: ", prefixed("delete metric: "), _handle_delete_metric),
    Mode("metrics", exact_match("metrics"), _handle_metrics),
    Mode("taxonomy", exact_match("taxonomy"), _handle_taxonomy),
    Mode("rubric dashboard", exact_match("rubric dashboard"), _handle_rubric_dashboard),
    Mode("explain: ", prefixed("explain: "), _handle_explain),
    Mode("audit: ", prefixed("audit: "), _handle_audit),
    Mode("notebook: ", prefixed("notebook: "), _handle_notebook_question),
    Mode("notebook last", exact_match("notebook last"), _handle_notebook_last),
]


def dispatch(raw: str) -> bool:
    """Tries every mode in MODES in order; the first trigger that matches has
    its handler called and this returns True. Returns False when nothing
    matches, so the caller (main.py) knows to fall through to the plain-
    question path — this function never runs that fallback itself, keeping
    "what happens when nothing matches" main.py's own, single responsibility.
    """
    for mode in MODES:
        parsed_args = mode.trigger(raw)
        if parsed_args is not None:
            mode.handler(parsed_args)
            return True
    return False
