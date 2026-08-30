"""
ETL analyst sub-agent: LangGraph tool definitions + node definitions.

Two tools, used by a standard ReAct loop (see build_etl_analyst_graph below):
- extract_load: plain function, no LLM — downloads a URL to a local folder.
- transform_load: thin @tool wrapper around utils.data_cleaning.clean_dataset(), the
  same shared cleaning core clean_data.py and the enhanced load_data.py both use.

Step 0 (download the dataset zip, unzip into data/NAME/) stays a human step per the
spec — no tool here logs into or scrapes an authenticated source. extract_load only
does a plain, unauthenticated HTTP GET against a URL it's given.
"""

from pathlib import Path

import requests
from langchain.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from models.etl_schema import ETLAnalystState
from utils.data_cleaning import clean_dataset
from utils.llm_pick import pick_llm

DOWNLOAD_TIMEOUT_SECONDS = 30


@tool
def extract_load(url: str, output_folder: str, format: str) -> str:
    """Download a file from a URL and save it into a local folder.

    Args:
        url: the direct URL to download from (must already be a plain, unauthenticated
            HTTP(S) resource — this tool never logs into or scrapes anything requiring
            authentication; that step is a human responsibility per the operation manual).
        output_folder: local folder to save the downloaded file into (created if it
            does not exist).
        format: the file extension/type to save as (e.g. "csv", "json", "zip"). Used to
            name the saved file when the URL itself doesn't end in a usable filename.

    Returns a short human-readable status string: either a success message naming the
    saved file path and its size, or a clear description of what went wrong (bad URL,
    timeout, non-200 response) — this function never raises for a real network/HTTP
    failure, it always returns a string either way.
    """
    try:
        response = requests.get(url, timeout=DOWNLOAD_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.exceptions.Timeout:
        return f"ERROR: download timed out after {DOWNLOAD_TIMEOUT_SECONDS}s for URL: {url}"
    except requests.exceptions.ConnectionError as e:
        return f"ERROR: could not connect to URL {url}: {e}"
    except requests.exceptions.HTTPError as e:
        return f"ERROR: server returned an error status for URL {url}: {e}"
    except requests.exceptions.RequestException as e:
        return f"ERROR: request to {url} failed: {e}"

    out_dir = Path(output_folder)
    out_dir.mkdir(parents=True, exist_ok=True)

    url_name = Path(url.split("?")[0]).name
    if url_name and "." in url_name:
        file_name = url_name
    else:
        file_name = f"downloaded_data.{format.lstrip('.')}"

    dest_path = out_dir / file_name
    dest_path.write_bytes(response.content)

    return f"Downloaded {url} -> {dest_path} ({len(response.content)} bytes)"


@tool
def transform_load(folder_path: str) -> str:
    """Check every CSV file in a folder against the data-cleaning rubric and clean any
    that need it, with a human approval gate before any generated code actually runs.

    Args:
        folder_path: local folder containing the raw CSV file(s) to check/clean.

    This is a thin wrapper around utils.data_cleaning.clean_dataset() — the same shared
    implementation clean_data.py and the enhanced load_data.py both use. The exact same
    approval gate and retry-on-failure logic applies here: no exceptions for this
    autonomous path. Returns clean_dataset()'s human-readable summary string.
    """
    result = clean_dataset(folder_path)
    return result.summary()


ETL_SYSTEM_PROMPT = """You are an ETL analyst. You have two tools:

- extract_load(url, output_folder, format): downloads a file from a URL into a local \
folder. Use this when asked to fetch/download data from a URL.
- transform_load(folder_path): checks CSV files in a folder for data-quality issues and \
cleans any that need it (with a human approval step before any cleaning code runs). Use \
this when asked to clean, transform, or prepare a folder of data.

Reason step by step about what the user's request actually requires. Call tools one at a \
time and look at each result before deciding the next step. If a request asks for both a \
download and cleaning, do the download first, then clean the folder it landed in. Once \
you've completed everything the request asked for, respond with a plain-English summary \
of what was actually done — do not call more tools than the request requires, and do not \
claim a step happened that a tool result didn't actually confirm."""


def call_model(state: ETLAnalystState) -> dict:
    """ReAct reasoning node: the LLM decides whether to call a tool next, based on the
    full message history so far. Uses pick_llm("high") — this loop makes real decisions
    about what to do next, not just clean-up work.

    The system prompt is prepended fresh on every call rather than stored in state —
    simplest way to guarantee it's always present without tracking "is this the first
    call" separately.
    """
    llm = pick_llm("high").bind_tools([extract_load, transform_load])
    response = llm.invoke([("system", ETL_SYSTEM_PROMPT), *state.messages])
    return {"messages": [response]}


MAX_ETL_STEPS = 15


def build_etl_analyst_graph():
    """Wire the standard ReAct loop using LangGraph's prebuilt ToolNode + tools_condition.

    Graph shape:
        START -> agent -> (tools_condition) -> tools -> agent (loop)
                                              -> END (once the LLM stops requesting tools)

    Callers must pass config={"recursion_limit": N} to .invoke() (see run_etl_analyst
    below) — LangGraph raises GraphRecursionError once the limit is hit rather than
    looping forever; that error is caught and turned into a clear message by the caller,
    not silently swallowed here.
    """
    graph = StateGraph(ETLAnalystState)

    graph.add_node("agent", call_model)
    graph.add_node("tools", ToolNode([extract_load, transform_load]))

    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", tools_condition, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")

    return graph.compile()


def run_etl_analyst(user_request: str, max_steps: int = MAX_ETL_STEPS) -> str:
    """Invoke the compiled graph on a single user request, with an explicit recursion
    limit so a bad decision pattern can't loop indefinitely.

    LangGraph's own recursion_limit counts graph super-steps, not individual node
    visits; multiplying by 2 gives room for max_steps real agent-decides/tool-runs
    round trips before hitting the cap, while still guaranteeing termination. On
    GraphRecursionError, returns a clear, deterministic message rather than letting the
    opaque LangGraph exception surface or silently returning nothing.
    """
    from langchain_core.messages import HumanMessage
    from langgraph.errors import GraphRecursionError

    graph = build_etl_analyst_graph()
    try:
        final_state = graph.invoke(
            ETLAnalystState(messages=[HumanMessage(content=user_request)]),
            config={"recursion_limit": max_steps * 2},
        )
    except GraphRecursionError:
        return (
            f"Stopped after {max_steps} steps without completing the request — the agent "
            "did not reach a final answer within the step limit. This may mean the request "
            "needs to be broken into smaller pieces, or the agent got stuck retrying the "
            "same action."
        )

    last_message = final_state["messages"][-1]
    content = last_message.content
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if not (isinstance(block, dict) and block.get("type") == "thinking")
        )
    return content
