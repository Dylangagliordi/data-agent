"""Fully Agentic Conductor (Spec 16): a top-level ReAct loop, built the exact
same way agents/etl_analyst.py already is (LangGraph's prebuilt ToolNode +
tools_condition — see that module for why this is a reusable LangGraph
pattern, not a new kind of mechanism), given a much larger toolbox: every
genuinely read-only, no-side-effect mode this project has, plus the ability
to ask a plain business question (which already covers visualization —
the router already detects chart-shaped questions internally, so there is no
separate "make a chart" tool to invent).

Deliberately conservative starting toolset (13 read-only report tools + 1
ask_question tool) — explicitly excluded, on purpose:
- define metric: / delete metric: — write to _saved_metrics with no approval
  gate of their own today (typing the command IS the confirmation); letting
  an autonomous loop trigger that write without a human explicitly typing it
  would be a genuinely new, ungated path.
- prepare: <table> for <goal> — each individual decision it makes is itself
  gated, so arguably safe, but it is itself an orchestration-shaped tool;
  stacking two layers of autonomous sequencing in v1 adds risk for no proven
  need yet. A natural v2 candidate once this is proven safe standalone.

Zero new approval-gate behavior: ask_question can still redirect into
clean_and_reload, or into Scratch Mode on the visualization path — both
already go through their own real, unchanged human approval gate
(utils.hitl.request_decision) regardless of who calls them. The conductor
decides WHICH tool runs next; it never gets to skip WHETHER a risky action
still needs a real "yes".
"""

from langchain.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from models.conductor_schema import ConductorState


@tool
def ask_question(question: str) -> str:
    """Ask a plain-English business question against the data — the exact
    same path as typing a question directly into the CLI. Covers everything
    a normal question already covers: reading data via SQL, and requesting a
    chart (the router already detects chart-shaped wording on its own — there
    is no separate visualization tool). Returns the real final answer text.

    If the underlying table needs cleaning first, or a visualization needs
    Scratch Mode's custom code, those still stop and ask a real human for
    approval exactly as they always do — this tool does not skip that.
    """
    from utils.cli_modes import run_question

    result = run_question(question)
    return result.get("final_answer", "(no answer produced)")


@tool
def profile_table(table_name: str) -> str:
    """Real per-column statistics for one table (null rates, distinct counts,
    numeric min/max/avg, or categorical top values) with no question needed —
    useful for getting oriented on a table before analyzing it. Returns the
    path to the rendered report."""
    from utils.auto_eda import render_auto_eda_html

    path = render_auto_eda_html(table_name)
    return f"Auto-EDA profile written to {path}"


@tool
def check_joins() -> str:
    """The real, whole-schema relationship map — which tables actually
    connect, on what column, and whether that join has fan-out (needs
    pre-aggregation). Useful before writing an analysis that spans more than
    one table. Returns the path to the rendered report."""
    from utils.join_advisory import render_join_advisory_html

    path = render_join_advisory_html()
    return f"Join advisory written to {path}"


@tool
def check_ingestion_sources() -> str:
    """Every URL this project's ETL tools have ever fetched, and how it went
    (success or a real recorded error). Returns the path to the rendered
    report."""
    from utils.ingestion_registry import render_ingestion_sources_html

    path = render_ingestion_sources_html()
    return f"Ingestion sources written to {path}"


@tool
def check_dq_backlog() -> str:
    """Every table with an outstanding data-quality issue, worst first.
    Returns the path to the rendered report."""
    from utils.dq_backlog import render_dq_backlog_html

    path = render_dq_backlog_html()
    return f"DQ backlog written to {path}"


@tool
def check_freshness() -> str:
    """Checks every tracked table's real source file for drift since it was
    last processed. Useful before trusting an analysis is based on current
    data. Returns the path to the rendered report."""
    from utils.freshness_briefing import render_freshness_briefing_html

    path = render_freshness_briefing_html()
    return f"Freshness briefing written to {path}"


@tool
def check_inventory() -> str:
    """The real, live ground truth of this project's own code — every graph
    node, every code module, every CLI command. Rarely useful for a business
    question; mainly for questions about the system itself. Returns the path
    to the rendered report."""
    from utils.doc_drift import render_inventory_html

    path = render_inventory_html()
    return f"Code inventory written to {path}"


@tool
def check_taxonomy() -> str:
    """Browses every saved version of a category grouping (from Manual Mode)
    and shows what changed between versions. Returns the path to the
    rendered report."""
    from utils.taxonomy_governance import render_taxonomy_governance_html

    path = render_taxonomy_governance_html()
    return f"Taxonomy governance written to {path}"


@tool
def check_rubric_dashboard() -> str:
    """How often each of this project's analyst-judgment rules has actually
    fired across every past run. Returns the path to the rendered report."""
    from utils.rubric_dashboard import render_rubric_dashboard_html

    path = render_rubric_dashboard_html()
    return f"Rubric dashboard written to {path}"


@tool
def check_table_audit(table_name: str) -> str:
    """One table's entire real history — every cleaning event and every
    transformation decision, across every run ever logged. Returns the path
    to the rendered report."""
    from utils.audit_export import render_table_audit_html

    path = render_table_audit_html(table_name)
    return f"Audit history written to {path}"


@tool
def explain_past_answer(question: str) -> str:
    """Finds the most recent PAST run of the exact given question and builds
    a report from it, without re-running anything. Only useful if this exact
    question has been asked before — use ask_question instead for a new
    question."""
    from utils.generate_report import generate_report
    from utils.run_comparison import find_entries_for_question

    entries = find_entries_for_question(question)
    if not entries:
        return f"No past run found for exactly: {question!r} — this question has never been asked before."
    path = generate_report(entries[-1])
    return f"Explanation written to {path}"


@tool
def compare_recent_runs(question: str) -> str:
    """Diffs the two most recent runs of the exact given question — did the
    SQL, the results, or the answer change between them. Only useful if this
    exact question has been asked at least twice before."""
    from utils.run_comparison import render_run_comparison_html

    path = render_run_comparison_html(question)
    if path is None:
        return f"Not enough history to compare — need at least 2 past runs of exactly: {question!r}"
    return f"Run comparison written to {path}"


@tool
def get_data_dictionary(table_name: str) -> str:
    """A one-page reference for one table: row count, quality status, and
    per-column type/key-role/derived-or-not/high-cardinality flags. Returns
    the path to the rendered report."""
    from utils.data_dictionary import render_data_dictionary_html

    path = render_data_dictionary_html(table_name)
    return f"Data dictionary written to {path}"


@tool
def list_saved_metrics() -> str:
    """Lists every currently defined canonical metric (named, reusable
    calculations). Returns the path to the rendered report."""
    from utils.load_data import ensure_saved_metrics_table, get_admin_connection, read_saved_metrics
    from utils.semantic_layer import render_semantic_layer_html

    conn = get_admin_connection()
    try:
        ensure_saved_metrics_table(conn)
        metrics = read_saved_metrics(conn)
    finally:
        conn.close()
    path = render_semantic_layer_html(metrics)
    return f"Semantic layer written to {path}"


CONDUCTOR_TOOLS = [
    ask_question,
    profile_table,
    check_joins,
    check_ingestion_sources,
    check_dq_backlog,
    check_freshness,
    check_inventory,
    check_taxonomy,
    check_rubric_dashboard,
    check_table_audit,
    explain_past_answer,
    compare_recent_runs,
    get_data_dictionary,
    list_saved_metrics,
]


CONDUCTOR_SYSTEM_PROMPT = """You are a data analysis conductor. You have a goal to \
accomplish, and a set of read-only tools to investigate with. Reason step by step \
about what you actually need to check before you can give a complete, well-supported \
answer — you do not need to use every tool, and you should not use a tool that isn't \
actually relevant to the goal.

ask_question is your main tool for actually reading data and getting real analytical \
answers (including charts — just phrase the question to ask for one, the same as a \
human would). The other tools are for orienting yourself: understanding a table's \
shape, checking data quality or freshness, understanding how tables relate, or looking \
at past history — use them BEFORE asking your main analytical question when they would \
genuinely inform it, not just to seem thorough.

None of your tools can write or change anything themselves — if an underlying action \
needs a human's approval (cleaning a table, running custom visualization code), that \
approval prompt will appear and this run will wait for a real answer, exactly as it \
would outside of you.

Once you have genuinely enough real information to answer the stated goal, stop calling \
tools and give a clear, complete final answer that draws on everything you actually \
found — do not call more tools than the goal requires, and never state something as a \
fact that a tool result did not actually confirm."""


def call_model(state: ConductorState) -> dict:
    """ReAct reasoning node: the LLM decides whether to call a tool next,
    based on the full message history so far. Uses pick_llm("high") — this
    loop makes real decisions about what to investigate next, not just
    clean-up work, the same reasoning tier generate_sql already uses."""
    from utils.llm_pick import pick_llm

    llm = pick_llm("high").bind_tools(CONDUCTOR_TOOLS)
    response = llm.invoke([("system", CONDUCTOR_SYSTEM_PROMPT), *state.messages])
    return {"messages": [response]}


MAX_CONDUCTOR_STEPS = 12


def build_conductor_graph():
    """Wire the standard ReAct loop using LangGraph's prebuilt ToolNode +
    tools_condition — the exact same shape agents/etl_analyst.py already
    uses, just with a much larger toolbox bound to the model call.

    Graph shape:
        START -> agent -> (tools_condition) -> tools -> agent (loop)
                                              -> END (once the LLM stops requesting tools)
    """
    graph = StateGraph(ConductorState)

    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode(CONDUCTOR_TOOLS))

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    return graph.compile()


def run_conductor(goal: str, max_steps: int = MAX_CONDUCTOR_STEPS) -> dict:
    """Invoke the compiled conductor graph on one goal, with an explicit
    recursion limit so an unproductive reasoning pattern can't loop forever —
    same discipline as agents/etl_analyst.py:run_etl_analyst.

    Returns {"final_answer": str, "tool_calls": [{"tool", "input", "output"}, ...]}
    — the real, ordered trace of every tool this run actually called, with
    its real input and real output, for Spec 16 Part 2's narrative assembly
    (nothing here is invented; a run that calls zero tools returns an empty
    trace, not a fabricated one).
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
    from langgraph.errors import GraphRecursionError

    graph = build_conductor_graph()
    try:
        final_state = graph.invoke(
            ConductorState(messages=[HumanMessage(content=goal)]),
            config={"recursion_limit": max_steps * 2},
        )
    except GraphRecursionError:
        return {
            "final_answer": (
                f"Stopped after {max_steps} steps without reaching a final answer — this "
                "goal may need to be broken into smaller pieces, or the conductor got stuck "
                "repeating the same investigation."
            ),
            "tool_calls": [],
        }

    messages = final_state["messages"]

    # Real trace reconstruction: pair each AIMessage's real tool_calls with the
    # real ToolMessage that answered it (matched by tool_call_id — the same
    # identifier LangChain itself uses to pair a call with its result, never
    # guessed by position).
    tool_calls_by_id = {
        m.tool_call_id: m.content for m in messages if isinstance(m, ToolMessage)
    }
    trace = []
    for m in messages:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for call in m.tool_calls:
                trace.append(
                    {
                        "tool": call["name"],
                        "input": call["args"],
                        "output": tool_calls_by_id.get(call["id"], ""),
                    }
                )

    last_message = messages[-1]
    content = last_message.content
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if not (isinstance(block, dict) and block.get("type") == "thinking")
        )

    return {"final_answer": content, "tool_calls": trace}
