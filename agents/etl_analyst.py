"""
ETL analyst sub-agent: LangGraph tool definitions + node definitions.

Three tools, used by a standard ReAct loop (see build_etl_analyst_graph below):
- extract_load: plain function, no LLM — downloads a URL to a local folder.
- scrape_load (Spec 10): plain function, no LLM — fetches a webpage and extracts the
  real data table embedded in its HTML, saving it as a CSV. Shares
  _fetch_with_ssrf_protection with extract_load so the SSRF safety boundary lives in
  exactly one place.
- transform_load: thin @tool wrapper around utils.data_cleaning.clean_dataset(), the
  same shared cleaning core clean_data.py and the enhanced load_data.py both use.

Step 0 (download the dataset zip, unzip into data/NAME/) stays a human step per the
spec — no tool here logs into or scrapes anything requiring authentication.
extract_load/scrape_load only ever do a plain, unauthenticated HTTP GET against a URL
they're given.
"""

import io
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from langchain.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition

from models.etl_schema import ETLAnalystState
from utils.data_cleaning import clean_dataset
from utils.format_normalization import largest_table
from utils.llm_pick import pick_llm

DOWNLOAD_TIMEOUT_SECONDS = 30

# SSRF protection (permanent safety boundary — see AGENTS.md). extract_load will
# fetch whatever URL the LLM decides to pass it; without this, that URL could
# point at a private/internal network address or a cloud-metadata endpoint
# (e.g. 169.254.169.254, which serves IAM credentials on AWS/GCP/Azure) and the
# tool would happily fetch it. Known cloud-metadata hostnames that don't
# already resolve to a link-local IP (so aren't already caught by the
# ip.is_link_local check below).
_BLOCKED_METADATA_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.goog",
}
_MAX_REDIRECTS = 5


def _resolve_all_ips(hostname: str) -> list:
    """Every distinct IP (v4 and v6) a hostname resolves to right now."""
    infos = socket.getaddrinfo(hostname, None)
    return list({info[4][0] for info in infos})


def _ip_is_blocked(ip_str: str) -> bool:
    """True for any IP that is not a genuine, routable public address —
    private ranges (10/8, 172.16/12, 192.168/16, fc00::/7, ...), loopback
    (127.0.0.1, ::1), link-local (169.254.0.0/16, fe80::/10 — this is what
    169.254.169.254, the AWS/GCP/Azure metadata endpoint, actually is),
    multicast, reserved, and unspecified (0.0.0.0)."""
    ip = ipaddress.ip_address(ip_str)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_fetch_url(url: str) -> tuple[bool, str]:
    """Reject a URL before any request is made if it targets a private/internal
    network address or a known cloud-metadata endpoint. Returns (is_safe, reason)
    — reason is "" when is_safe is True. DNS is resolved here (not left to the
    HTTP client) specifically so the check happens against the real IP the
    hostname currently points to, not just the hostname text.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"only http/https URLs are allowed, got scheme {parsed.scheme!r}"
    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname"
    if hostname.lower() in _BLOCKED_METADATA_HOSTNAMES:
        return False, f"{hostname!r} is a known cloud-metadata endpoint"
    try:
        ips = _resolve_all_ips(hostname)
    except socket.gaierror as e:
        return False, f"could not resolve host {hostname!r}: {e}"
    if not ips:
        return False, f"host {hostname!r} did not resolve to any IP address"
    for ip_str in ips:
        if _ip_is_blocked(ip_str):
            return False, (
                f"host {hostname!r} resolves to {ip_str}, a private/internal/"
                "metadata address — fetching internal network resources is blocked"
            )
    return True, ""


def _fetch_with_ssrf_protection(url: str):
    """Shared SSRF-safe fetch used by both extract_load and scrape_load:
    validates the URL and every redirect hop before following it (see
    _validate_fetch_url), resolving DNS fresh each time — the same
    permanent safety boundary (see AGENTS.md), now living in exactly one
    place rather than duplicated across two tools with their own fetch
    logic. Returns (response, "") on success, or (None, error_message) on
    any failure; the caller decides the exact "ERROR: ..." string to
    return to the LLM, since extract_load and scrape_load phrase a
    download failure and a scrape failure slightly differently.
    """
    is_safe, reason = _validate_fetch_url(url)
    if not is_safe:
        return None, f"refused to fetch URL — {reason}"

    current_url = url
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            response = requests.get(
                current_url, timeout=DOWNLOAD_TIMEOUT_SECONDS, allow_redirects=False
            )
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                if not location:
                    break
                next_url = urljoin(current_url, location)
                is_safe, reason = _validate_fetch_url(next_url)
                if not is_safe:
                    return None, f"refused to follow redirect — {reason}"
                current_url = next_url
                continue
            response.raise_for_status()
            return response, ""
        return None, f"too many redirects (> {_MAX_REDIRECTS}) while fetching URL: {url}"
    except requests.exceptions.Timeout:
        return None, f"download timed out after {DOWNLOAD_TIMEOUT_SECONDS}s for URL: {url}"
    except requests.exceptions.ConnectionError as e:
        return None, f"could not connect to URL {url}: {e}"
    except requests.exceptions.HTTPError as e:
        return None, f"server returned an error status for URL {url}: {e}"
    except requests.exceptions.RequestException as e:
        return None, f"request to {url} failed: {e}"


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
    blocked target, timeout, non-200 response) — this function never raises for a real
    network/HTTP failure, it always returns a string either way.

    Use this when the data is available at a direct, downloadable URL (a link that
    itself IS the file). If the data is only embedded in a webpage's HTML (a table
    rendered on a page, with no direct file link), use scrape_load instead.

    SSRF protection (permanent safety boundary — see AGENTS.md): before fetching, and
    again before following any redirect, the target URL's hostname is resolved and
    rejected if it points at a private/internal IP range or a known cloud-metadata
    endpoint (e.g. 169.254.169.254). This is checked here rather than trusted to the
    caller because this tool is invoked by an LLM deciding what URL to pass, with no
    allowlist of its own.

    Spec 10, Part 3: records this attempt (success or failure) in _ingestion_sources
    — a durable memory of every URL this tool has ever been asked to fetch, purely a
    registry/audit trail, never a gate on whether the fetch is allowed to happen.
    """
    result = _do_extract_load(url, output_folder, format)
    _record_ingestion(url, output_folder, "download", result)
    return result


def _do_extract_load(url: str, output_folder: str, format: str) -> str:
    response, error = _fetch_with_ssrf_protection(url)
    if response is None:
        return f"ERROR: {error}"

    out_dir = Path(output_folder)
    out_dir.mkdir(parents=True, exist_ok=True)

    url_name = Path(response.url.split("?")[0]).name
    if url_name and "." in url_name:
        file_name = url_name
    else:
        file_name = f"downloaded_data.{format.lstrip('.')}"

    dest_path = out_dir / file_name
    dest_path.write_bytes(response.content)

    return f"Downloaded {url} -> {dest_path} ({len(response.content)} bytes)"


def _record_ingestion(url: str, output_folder: str, source_kind: str, result: str) -> None:
    """Shared by extract_load and scrape_load: records one fetch attempt into
    _ingestion_sources, regardless of whether it succeeded — a source that's
    been failing repeatedly is exactly the kind of thing this registry exists
    to make visible. Grants the ETL agent access to exactly one more internal
    bookkeeping table (like transform_load's use of _cleaning_recipes) — never
    user data, never a live table."""
    from utils.load_data import ensure_ingestion_sources_table, get_admin_connection, record_ingestion_attempt

    status = "error" if result.startswith("ERROR:") else "ok"
    last_error = result if status == "error" else ""
    conn = get_admin_connection()
    try:
        ensure_ingestion_sources_table(conn)
        record_ingestion_attempt(conn, url, output_folder, source_kind, status, last_error)
    finally:
        conn.close()


@tool
def scrape_load(url: str, output_folder: str) -> str:
    """Fetch a webpage and extract the real data table embedded in its HTML,
    saving it as a CSV — for a source that has no direct downloadable file,
    only a table rendered on a page.

    Args:
        url: the page URL to scrape (must be a plain, unauthenticated HTTP(S) page
            — same restriction as extract_load; this tool never logs into or
            scrapes anything requiring authentication).
        output_folder: local folder to save the extracted table into (created if
            it does not exist).

    Unlike extract_load (which saves whatever bytes a URL returns, unexamined),
    this actually parses the response as HTML and looks for <table> elements. A
    real page often has more than one (navigation, footer, ads) — the LARGEST
    table by cell count (rows x columns) is taken as the real data table
    (utils.format_normalization.largest_table, the same explicit, stated rule
    used when normalizing an already-downloaded HTML file), never "whichever
    table happens to appear first." Never invents columns or rows beyond what
    pandas' own HTML table parser actually extracts from the real page content.

    Returns a clear "ERROR: ..." string on any failure (fetch blocked/failed, no
    table found) rather than raising — this tool never raises for a real
    network/parsing failure, it always returns a string either way.

    SSRF protection: uses the exact same SSRF-safe fetch as extract_load (see
    _fetch_with_ssrf_protection / AGENTS.md's SSRF Protection section) — this
    tool is invoked by an LLM deciding what URL to pass, with no allowlist of
    its own, so the same permanent safety boundary applies here unchanged.

    Spec 10, Part 3: records this attempt (success or failure) in
    _ingestion_sources — see extract_load's own docstring for why.
    """
    result = _do_scrape_load(url, output_folder)
    _record_ingestion(url, output_folder, "scrape", result)
    return result


def _do_scrape_load(url: str, output_folder: str) -> str:
    response, error = _fetch_with_ssrf_protection(url)
    if response is None:
        return f"ERROR: {error}"

    try:
        tables = pd.read_html(io.StringIO(response.text), flavor="lxml")
    except ValueError as e:
        return f"ERROR: no table found on page {url}: {e}"

    if not tables:
        return f"ERROR: no table found on page {url}"

    df = largest_table(tables)

    out_dir = Path(output_folder)
    out_dir.mkdir(parents=True, exist_ok=True)

    url_name = Path(response.url.split("?")[0]).name
    base_stem = Path(url_name).stem if url_name else "scraped_table"
    dest_path = out_dir / f"{base_stem or 'scraped_table'}.csv"
    df.to_csv(dest_path, index=False)

    return (
        f"Scraped {url} -> {dest_path} "
        f"({df.shape[0]} rows, {df.shape[1]} columns; largest of {len(tables)} table(s) found on the page)"
    )


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

    Spec 7b: opens its own short-lived admin connection (same pattern
    agents/sql_analyst.py:clean_and_reload and utils/load_data.py:main() already use)
    so a signature-eligible fix approved once for a recurring dataset is replayed
    instead of re-generated and re-approved on every ask — this tool was the one
    real entry point Spec 7 itself left out. No reload-invalidation call is needed
    here: transform_load only cleans a folder, it never reloads a table into
    Postgres, so there's no genuine-reload event for this tool to hook — a real
    reload elsewhere (load_data.py, clean_and_reload) still invalidates correctly
    regardless of which path originally approved the cached fix. This grants the
    ETL agent access to exactly one internal bookkeeping table — never user data,
    never a live table, never _data_quality_status.

    Spec 10: before cleaning, converts any JSON/Excel/HTML file at the top level
    of folder_path into a sibling CSV (utils.format_normalization) — clean_dataset()
    only ever globs for *.csv, so a non-CSV file (e.g. one scrape_load or
    extract_load just saved) would otherwise be invisible to it. Never overwrites
    an existing .csv, and never touches clean_dataset() itself.
    """
    from utils.format_normalization import normalize_folder_to_csv
    from utils.load_data import ensure_cleaning_recipes_table, get_admin_connection

    normalization = normalize_folder_to_csv(folder_path)

    conn = get_admin_connection()
    try:
        ensure_cleaning_recipes_table(conn)
        result = clean_dataset(folder_path, recipe_conn=conn)
    finally:
        conn.close()

    summary = result.summary()
    if normalization["written"]:
        names = ", ".join(p.name for p in normalization["written"])
        summary += f"\n\nAlso converted to CSV before cleaning: {names}."
    if normalization["errors"]:
        summary += f"\n\nCould not convert to CSV: {'; '.join(normalization['errors'])}."
    return summary


ETL_SYSTEM_PROMPT = """You are an ETL analyst. You have three tools:

- extract_load(url, output_folder, format): downloads a file from a URL into a local \
folder. Use this when the data is available at a direct, downloadable link (the URL \
itself points straight at a file).
- scrape_load(url, output_folder): fetches a webpage and extracts the real data table \
embedded in its HTML, saving it as a CSV. Use this when the data is only rendered on a \
page (a table on a webpage) with no direct downloadable file link.
- transform_load(folder_path): checks CSV files in a folder for data-quality issues and \
cleans any that need it (with a human approval step before any cleaning code runs). Also \
converts any JSON/Excel/HTML file in the folder to CSV first, so a file saved by \
extract_load in a non-CSV format is still picked up. Use this when asked to clean, \
transform, or prepare a folder of data.

Reason step by step about what the user's request actually requires. Call tools one at a \
time and look at each result before deciding the next step. If a request asks for both a \
download/scrape and cleaning, do the download/scrape first, then clean the folder it \
landed in. Once you've completed everything the request asked for, respond with a \
plain-English summary of what was actually done — do not call more tools than the \
request requires, and do not claim a step happened that a tool result didn't actually \
confirm."""


def call_model(state: ETLAnalystState) -> dict:
    """ReAct reasoning node: the LLM decides whether to call a tool next, based on the
    full message history so far. Uses pick_llm("high") — this loop makes real decisions
    about what to do next, not just clean-up work.

    The system prompt is prepended fresh on every call rather than stored in state —
    simplest way to guarantee it's always present without tracking "is this the first
    call" separately.
    """
    llm = pick_llm("high").bind_tools([extract_load, scrape_load, transform_load])
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
    graph.add_node("tools", ToolNode([extract_load, scrape_load, transform_load]))

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
