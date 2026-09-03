---
description: >
  Reference for this project's existing state schemas, log file formats, database
  structure, and code organization. Load when modifying agents/, utils/, or models/,
  or when working with query_log.jsonl, cleaning_log.jsonl, or _data_quality_status.
---

# Data-Agent Architecture Reference

Compact, accurate snapshot of the real current state. Read this instead of
re-exploring files from scratch. All facts are sourced from the actual files as
of the last update.

---

## 1. State Schemas

### `SQLAnalystState` — `models/schema.py`

Shared state threaded through every node of the SQL analyst LangGraph.

| Field | Type | What it holds |
|---|---|---|
| `messages` | `list` (add_messages) | Accumulated LangChain messages (HumanMessage / AIMessage) |
| `user_question` | `str` | Raw question string from the user |
| `curated_question` | `str` | Grammar/spelling-corrected question (no intent change) |
| `prompt_query_context` | `str` | Full schema context string built by add_context for the LLM |
| `generated_sql_query` | `str` | SQL text produced by generate_sql (may be updated on retry) |
| `is_safe` | `Literal["yes","no"]` | Safety judge verdict |
| `comments` | `str` | Safety judge reasoning text |
| `sql_query_execution_result` | `str` | Raw `str()` repr of query results, or error string prefixed `SQL_EXECUTION_ERROR:`, or truncation-marked string |
| `final_answer` | `str` | Plain-English answer (set by represent_final_answer, cancel_sql, or build_visualization) |
| `data_quality_warnings` | `list` | List of `{"table": str, "warning": str}` dicts for fail-level or no-record tables |
| `sql_attempts` | `int` | Count of generate→execute cycles; capped at `MAX_SQL_ATTEMPTS = 5` |
| `data_quality_action` | `Literal["proceed","needs_cleaning"]` | Set by add_context; "needs_cleaning" routes to clean_and_reload |
| `tables_to_clean` | `list` | List of `{"table": str, "source_folder": str}` dicts for tables that need auto-cleaning |
| `cleaning_attempted_tables` | `list` | Table names already attempted this question (stop condition for clean loop) |
| `wants_visualization` | `bool` | True only when the router dispatches to visualize_node |
| `chart_type` | `str` | e.g. "bar chart", "scatter plot", "treemap" |
| `chart_type_source` | `Literal["explicit","reasoned"]` | Whether the user named the chart type or the LLM chose it |
| `chart_type_reasoning` | `str` | Non-empty only when `chart_type_source == "reasoned"` |
| `output_file_path` | `str` | Absolute path to the written CSV file |
| `export_target` | `Literal["csv","tableau"]` | "tableau" only when curated_question explicitly names Tableau |
| `chart_image_path` | `str` | Absolute path to the rendered `.png` file (empty string if rendering failed or not a visualization run) |

**Auxiliary structured-output schemas (not in main state):**
- `JudgeSchema` (`models/schema.py`): `answer: Literal["yes","no"]`, `comments: str` — used by `is_safe` node only.
- `ChartTypeSchema` (`models/schema.py`): `chart_type: str`, `chart_type_source`, `chart_type_reasoning` — used by `determine_chart_type` node only.

---

### `DataAgentSchema` — `models/router_schema.py`

Thin state layer for the top-level router graph. Sub-agent internal fields never appear here.

| Field | Type | What it holds |
|---|---|---|
| `messages` | `list` (add_messages) | Incoming message history |
| `route_response` | `str` | Router classification: `"sql_analyst"`, `"etl_analyst"`, or `"visualize"` |
| `route_comments` | `str` | Router's reasoning for its classification |
| `final_answer` | `str` | Final answer propagated up from whichever sub-agent ran |

**`RouterSchema`** (`models/router_schema.py`): `answer: Literal["sql_analyst","etl_analyst","visualize"]`, `comments: str` — used by `router_node` via `with_structured_output` only.

---

### `ETLAnalystState` — `models/etl_schema.py`

Minimal ReAct state — only a message list. No named intermediate fields.

| Field | Type | What it holds |
|---|---|---|
| `messages` | `list` (add_messages) | Growing conversation for the ReAct loop |

---

## 2. Log File Formats

### `logs/query_log.jsonl`

One JSON line per run. Shape varies by `route_response`.

**`sql_analyst` entry keys:**
```
timestamp, route_response, route_comments, user_question,
curated_question, generated_sql_query, is_safe, comments,
sql_query_execution_result, final_answer
```

**`visualize` entry keys** (superset of sql_analyst, adds visualization fields):
```
timestamp, route_response, route_comments, user_question,
curated_question, chart_type, chart_type_source, chart_type_reasoning,
generated_sql_query, is_safe, sql_query_execution_result,
output_file_path, chart_image_path, final_answer
```
`chart_image_path` is present and non-empty when `build_visualization` succeeded; empty string when rendering failed. Entries logged before the image-rendering feature (commit c9ebda2) lack this key entirely — report code treats its absence as "no image available".

**`etl_analyst` entry keys:**
```
timestamp, route_response, route_comments, user_question, final_answer
```

`sql_query_execution_result` is the raw `str()` repr of a list-of-dicts from psycopg2. Truncated results are prefixed with `[TRUNCATED TO FIRST {N} ROWS — ...]`. Errors are prefixed with `SQL_EXECUTION_ERROR:`.

**Written by:** `main.py:log_run()`. SQL/visualize internal fields come from `agents/router.py:LAST_SQL_ANALYST_STATE` side-channel (module-level dict set by `sql_node` / `visualize_node` after every graph invocation).

---

### `logs/cleaning_log.jsonl`

One JSON line per `clean_dataset()` call.

**Top-level keys:**
```json
{
  "timestamp": "2026-09-02T20:05:07.031931+00:00",
  "source_folder": "data/data-science-jobs",
  "trigger": "manual",
  "files": [ ...file_rec objects... ]
}
```
`trigger` values: `"manual"` (clean_data.py or direct call), `"auto_redirect"` (clean_and_reload node in sql_analyst graph).

**Per-file record (`file_rec`) keys:**
```json
{
  "file_name": "Uncleaned_DS_jobs.csv",
  "table_name": "Uncleaned_DS_jobs",
  "status": "cleaned",
  "issues_found": [{"issue": "Placeholder values: ...", "severity": "fail"}, ...],
  "row_count_before": 672,
  "row_count_after": 672,
  "row_loss_flagged": false,
  "issues_resolved": ["Placeholder values: ...", ...],
  "issues_still_unresolved": [],
  "fail_issues": [{"issue": "...", "status": "resolved", "reasoning_comments": ["# comment"]}],
  "warn_batch": {"issues": [...], "status": "resolved", "reasoning_comments": [...]}
}
```
`status` values: `"cleaned"`, `"skipped_declined"`, `"skipped_no_issues"`, `"skipped_no_llm"`.
`warn_batch` is `null` when no warn-level issues existed.
`table_name` uses the original CSV stem (not necessarily lowercase) — report code normalizes to lowercase when looking up DB tables.

**Written by:** `utils/data_cleaning.py:_append_cleaning_log()`.

---

### `_data_quality_status` Postgres Table

Created and maintained by `utils/load_data.py:ensure_data_quality_status_table()`.

```sql
CREATE TABLE _data_quality_status (
    table_name   TEXT PRIMARY KEY,
    last_loaded_at TIMESTAMPTZ NOT NULL,
    status       TEXT NOT NULL,          -- "pass", "warn", or "fail"
    issues_found JSONB NOT NULL,         -- list of {"issue": str, "severity": str}
    was_cleaned  BOOLEAN NOT NULL,
    source_folder TEXT                   -- added after initial schema; NULL = can't auto-redirect
);
```

`status` is set by `utils/load_data.py:compute_quality_status()` which calls `_issue_severity()` from `utils/data_cleaning.py`. The table is excluded from `information_schema` queries in `add_context` (filtered by `table_name != '_data_quality_status'`). Only `fail`-status tables inject WARNING lines into `generate_sql`'s context; `warn`-status tables are silently ignored.

---

## 3. Code Organization

### `agents/sql_analyst.py` — SQL analyst graph nodes and helpers

| Function | Role |
|---|---|
| `curate_question` | Node 1: grammar/spelling fix only, no intent change. Uses `pick_llm("cheap")`. |
| `add_context` | Node 2 (no LLM): builds schema context from `information_schema`, runs fan-out check and DQ status check, sets `data_quality_action`. |
| `determine_chart_type` | Node (visualization path only): classifies chart type via `ChartTypeSchema` with `pick_llm("cheap")`. |
| `generate_sql` | Node 3: writes one SQL query. Uses `pick_llm("high")`. Extended with chart-shaping instructions on visualization path. |
| `is_safe` | Node 4: read-only safety gate via `JudgeSchema` with `pick_llm("cheap")`. |
| `execute_sql` | Node 6: runs SQL via `app_reader`, caps results at `MAX_RESULT_ROWS = 200`, handles errors and retry counting. |
| `cancel_sql` | Node 7: writes final_answer explaining blocked query. |
| `represent_final_answer` | Node 8: plain-English summarization via `pick_llm("cheap")`. Bypassed deterministically for truncated results. |
| `build_visualization` | Visualization terminal node: writes CSV (always), `.hyper` (if Tableau), renders `.png` via `_render_chart_image`, produces final_answer. |
| `clean_and_reload` | Auto-clean redirect node: calls `clean_dataset()` → `load_csv_to_table()` → updates `_data_quality_status`. Accepts `_llm=` for test injection. |
| `build_sql_analyst_graph` | Compiles and returns the compiled LangGraph `StateGraph`. |
| `_render_chart_image` | Renders a matplotlib `.png` for the given chart_type. Uses `Figure`/`FigureCanvasAgg` (no global pyplot). Catches all exceptions; returns `None` on failure. |
| `_squarify_rects` | Internal treemap layout (no squarify dependency). |
| `_detect_fanout_warnings` | Deterministic fan-out check: live `COUNT(*)/COUNT(DISTINCT)` queries against real data. |
| `_parse_sql_result` | Parses psycopg2 `str()` repr; handles `Decimal` and `datetime` types via controlled `eval()`. |
| `_chart_shaping_instruction` | Returns SQL-shaping instructions per chart type for `generate_sql`'s prompt. |

**SQL analyst graph wiring:**
```
START → curate_question → add_context
add_context →(route_after_add_context)→ generate_sql | determine_chart_type | clean_and_reload
clean_and_reload → add_context  (loops; stop via cleaning_attempted_tables)
determine_chart_type → generate_sql
generate_sql → is_safe →(route_after_safety_check)→ execute_sql | cancel_sql
execute_sql →(route_after_execute_sql)→ generate_sql (retry) | represent_final_answer | build_visualization
cancel_sql → END
represent_final_answer → END
build_visualization → END
```

---

### `agents/router.py` — Top-level dispatch

| Symbol | Role |
|---|---|
| `LAST_SQL_ANALYST_STATE` | Module-level `dict` side-channel; set by `sql_node`/`visualize_node` after every invocation so `main.py:log_run()` can read sub-agent internals without DataAgentSchema carrying them. Reset to `{}` at the start of each call. |
| `router_node` | Classifies message via `RouterSchema` / `pick_llm("cheap")`. |
| `sql_node` | Invokes `_SQL_ANALYST_GRAPH` with `wants_visualization=False`. |
| `visualize_node` | Invokes `_SQL_ANALYST_GRAPH` with `wants_visualization=True`. |
| `etl_node` | Invokes `_ETL_ANALYST_GRAPH` (ReAct loop). |
| `router_edge` | Conditional edge returning `state.route_response`. |
| `_SQL_ANALYST_GRAPH` / `_ETL_ANALYST_GRAPH` | Module-level compiled graphs — built once at import, not per call. |

---

### `agents/data_agent.py` — Top-level graph

`build_data_agent_graph()`: wires `router_node → {sql_node, etl_node, visualize_node} → END` using `DataAgentSchema`. Invoked by `main.py`.

---

### `agents/etl_analyst.py` — ETL ReAct agent

| Symbol | Role |
|---|---|
| `extract_load` | `@tool`: plain HTTP GET download to a local folder. Never authenticates. |
| `transform_load` | `@tool`: thin wrapper around `utils/data_cleaning.clean_dataset()`. |
| `call_model` | ReAct node: single `pick_llm("cheap")` call with tools bound. |
| `build_etl_analyst_graph` | Returns compiled ReAct graph (ToolNode + tools_condition). |

---

### `utils/data_cleaning.py` — Cleaning core

| Symbol | Role |
|---|---|
| `clean_dataset(folder_path, llm=None, trigger="manual")` | Main public entry point. Accepts `llm=` for test injection of a fake LLM (otherwise calls `pick_llm("high")`). Returns `CleaningResult`. |
| `check_rubric(file_path)` | Runs all `_check_*` functions against one CSV file; returns list of issue strings. |
| `FAIL_LEVEL_PREFIXES` | Tuple of prefixes mapping issue text to `"fail"` severity (14 entries — see below). |
| `WARN_LEVEL_PREFIXES` | Tuple of prefixes mapping issue text to `"warn"` severity (13 entries — see below). |
| `_issue_severity(issue)` | Classifies a single issue string as `"fail"`, `"warn"`, or `"unknown"` by prefix match. |
| `unresolved_issues_for_record(rec)` | Returns `rec.remaining_issues` as a list; used by `clean_and_reload` to feed `compute_quality_status`. |
| `CleaningResult` | Dataclass: `folder_path`, `cleaned_dir`, `cleaned_files`, `skipped_files`, `untouched_files`. |
| `FileCleaningRecord` | Dataclass: per-file result including `file_name`, `status`, `issues`, `remaining_issues`, `fail_issue_records`, `warn_batch`, `row_count_before/after`, `row_loss_flagged`. |
| `_clean_issue_group` | Generates and executes fix code for a batch of issues. Calls `_request_approval` for interactive approval gate. |

**`FAIL_LEVEL_PREFIXES`** (issues that produce `status="fail"` in `_data_quality_status`):
`"Duplicate rows:"`, `"Duplicate values:"`, `"Wrong data type:"`, `"Invalid values:"`, `"Encoding problem:"`, `"Structural issue:"`, `"Placeholder values:"`, `"Lost leading zeros:"`, `"Locale-specific number formatting:"`, `"Spreadsheet artifacts:"`, `"Column misalignment:"`, `"Header row duplicated mid-file:"`, `"Byte-order-mark:"`, `"Dangling references:"`

**`WARN_LEVEL_PREFIXES`** (issues that produce `status="warn"`):
`"Missing values:"`, `"Inconsistent categorical values:"`, `"Formatting noise:"`, `"Inconsistent boolean representations:"`, `"Currency/unit symbols:"`, `"Excessive floating-point noise:"`, `"Non-printable characters:"`, `"Inconsistent delimiters:"`, `"Column header issues:"`, `"Trailing empty rows:"`, `"Trailing empty column:"`, `"Special characters in headers:"`, `"Inconsistent granularity:"`

---

### `utils/load_data.py` — Admin-only data loader

| Function | Role |
|---|---|
| `get_admin_connection()` | Opens psycopg2 connection as admin/superuser. **Only file that uses this.** No password needed (local trust auth). Env vars: `PG_HOST`, `PG_PORT`, `PG_DATABASE`, `PG_ADMIN_USER`. |
| `ensure_data_quality_status_table(conn)` | Creates `_data_quality_status` if missing; runs `ADD COLUMN IF NOT EXISTS source_folder` migration idempotently. |
| `compute_quality_status(unresolved_issues)` | Returns `(status, issues_found_payload)` using `_issue_severity` from `data_cleaning.py`. Status: `"fail"` > `"warn"` > `"pass"`. |
| `write_data_quality_status(conn, table_name, status, issues_found, was_cleaned, source_folder=None)` | Upserts one row in `_data_quality_status`. |
| `load_csv_to_table(conn, csv_path, sample_rows_for_typing=500)` | Loads a CSV into Postgres via COPY, dropping and recreating the table. |
| `sanitize_identifier(name)` | Lowercases and strips a CSV stem to make a valid Postgres identifier (used as table name). |

---

### `utils/db.py` — Read-only connection helper

`get_app_reader_connection()`: connects as `PG_APP_READER_USER` with `PG_APP_READER_PASSWORD`. Used by every graph node that reads data (`add_context`, `execute_sql`, `_detect_fanout_warnings`, and all report helpers). Env vars loaded from `~/.hermes/profiles/data-agent/.env`.

---

### `utils/generate_report.py` — HTML report generator

Public API: `generate_report(entry: dict) -> str` (returns path to written `.html`). `last_query_log_entry() -> dict | None`.

Key internal functions:

| Function | Role |
|---|---|
| `_section_visualization(entry)` | Embeds chart image as inline base64 `<img>` if `chart_image_path` present; otherwise states "no chart image available". Extracts query assumptions via `_extract_query_assumptions`. |
| `_section_data_cleaning(cleaning_map)` | Shows issues/resolved/unresolved per table + a live before/after comparison computed by `_compute_before_after`. |
| `_extract_query_assumptions(sql_query)` | Parses WHERE/HAVING for non-obvious filters: `<> 'placeholder'`, `NOT IN (literals)`, `~ 'regex'`, `HAVING COUNT(*) >= N`. Returns plain-English strings. |
| `_compute_before_after(db_table, biggest)` | Runs two real `COUNT(*)` queries against the live DB to produce a before/after row-count comparison for a placeholder issue. Uses `get_app_reader_connection()`. |
| `_parse_placeholder_issue(issue_text)` | Parses cleaning log issue text to extract `{"column", "count", "placeholders"}`. |

---

### `utils/llm_pick.py` — Model tier routing

`pick_llm(level)` returns a chat model:
- `"cheap"` → `ChatAnthropic(model="claude-haiku-4-5", temperature=0)` — curate_question, is_safe, represent_final_answer, router_node, determine_chart_type, ETL call_model.
- `"high"` → `ChatAnthropic(model="claude-sonnet-5")` — generate_sql only. (No `temperature` param — rejected by this model.)
- `"low"` / `"medium"` → `ChatOllama(model="qwen3.5:4b", temperature=0, reasoning=False, num_predict=1024)` — not currently used by any active node; kept for future work.

---

## 4. Database Structure

**Two-role setup:**
- `app_reader` — read-only. All graph runtime queries use this role (`utils/db.py:get_app_reader_connection()`). Never write to this role's grants to make something work.
- Admin/superuser — write access. Used exclusively by `utils/load_data.py:get_admin_connection()`. Also used by `clean_and_reload` node (which calls `load_data.py` functions).

**Severity mapping location:** `utils/data_cleaning.py` — `FAIL_LEVEL_PREFIXES` and `WARN_LEVEL_PREFIXES` tuples at module level (lines 177–208). `compute_quality_status` in `utils/load_data.py` calls `_issue_severity` imported from `data_cleaning.py` — same mapping, one source of truth.

**Output directories:**
- `outputs/visualizations/` — CSV and PNG files from `build_visualization`. Naming: `{40-char-slug}_{YYYYMMDD_HHMMSS}.{csv,png}`.
- `reports/` — HTML reports from `generate_report`.
- `logs/` — `query_log.jsonl` and `cleaning_log.jsonl`.

---

## 5. Testing Conventions

**File naming:** `tests/test_<feature>.py` — standalone executable scripts, not pytest. Run with `PYTHONPATH=/path/to/data-agent uv run python tests/test_<feature>.py`.

**`_llm=None` / `llm=None` injection pattern:** Both `clean_dataset(folder_path, llm=None, ...)` and `clean_and_reload(state, _llm=None)` accept a fake LLM object in tests. When `None` (always in production), they call `pick_llm("high")`. Tests pass deterministic fake LLMs (e.g. `FakeDedupLLM`, `AlwaysFailLLM`, `AnyCodeLLM`) to control code generation without live API calls. Used in: `test_auto_clean_redirect.py`, `test_data_cleaning_retry.py`, `test_data_cleaning_post_validation.py`, `test_data_cleaning_fail_warn_split.py`, `test_data_quality_status.py`, `test_visualize_auto_clean.py`.

**Data fixtures:** Test data lives under `data/_test_etl/` — separate subdirectory per scenario (`clean_only/`, `dq_fail/`, `dq_warn/`, `approve_yes/`, etc.). Tests that require interactive approval (`test_data_cleaning_approve.py`) require stdin to be piped — they fail with `EOFError` when run non-interactively; this is expected behavior, not a regression.

**`PYTHONPATH` requirement:** All tests must be run from the project root with `PYTHONPATH=/Users/dylangagliordi/data-agent` set (or equivalent), since `agents/`, `models/`, and `utils/` are not installed packages.
