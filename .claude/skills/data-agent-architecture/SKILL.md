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
| `sql_query_execution_result` | `str` | Structured JSON `{"columns":[...],"rows":[[...],...],"truncated":bool}` for successful queries, or error string prefixed `SQL_EXECUTION_ERROR:` |
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
| `sql_analyst_trace` | `dict` | The SQL analyst sub-agent's full internal result dict (curated_question, generated_sql_query, is_safe, comments, sql_query_execution_result, and on the visualize path chart_type/output_file_path/chart_image_path), returned by `sql_node`/`visualize_node` as part of their own node output. Replaces the old `agents/router.py:LAST_SQL_ANALYST_STATE` module-level global (architecture review point #27) — that global was unsafe if two questions were ever handled concurrently, since one request's trace could be overwritten by another's before the first caller read it. `main.py:log_run()` reads this field directly off the graph's returned state instead. |

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

`sql_query_execution_result` is a structured JSON string `{"columns":[...],"rows":[[...],...],"truncated":bool}` for successful queries (Decimal serialized as float, datetime as ISO string). Errors are an unquoted string prefixed `SQL_EXECUTION_ERROR:`. Old log entries (pre-security-hardening commit) may contain the legacy `str()` repr or `[TRUNCATED TO FIRST …]` format.

**Written by:** `main.py:log_run()`. SQL/visualize internal fields come from `result.get("sql_analyst_trace", {})` — the `DataAgentSchema.sql_analyst_trace` field `sql_node`/`visualize_node` return as part of their own node output (see `DataAgentSchema` above; not a module-level global).

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
    source_folder TEXT,                  -- added after initial schema; NULL = can't auto-redirect
    source_checksum TEXT                 -- added for source-freshness tracking; NULL = no baseline
);
```

`status` is set by `utils/load_data.py:compute_quality_status()` which calls `_issue_severity()` from `utils/data_cleaning.py`. The table is excluded from `information_schema` queries in `add_context` (filtered by `table_name NOT IN ('_data_quality_status', '_fanout_status')`). Only `fail`-status tables inject WARNING lines into `generate_sql`'s context; `warn`-status tables are silently ignored.

`source_checksum` is the raw source file's real SHA-256 hex digest at the time it was last processed (`utils/load_data.py:compute_file_checksum`). Compared against the file's current bytes by `check_source_freshness()`, called from three places: `add_context` (every table with a known `source_folder`, regardless of current status — architecture review point #22), `clean_and_reload` (per targeted table, informational logging only, since those tables are already being reloaded regardless), and `load_data.py:main()` (per file, informational logging only). In `add_context`, a genuine mismatch on a `"pass"`/`"warn"`-status table is a real gate: it forces `tables_to_clean`/`"needs_cleaning"` exactly like a `"fail"` status would, with a `"...source changed, forcing fresh clean."` warning — a stale status row is never silently reused once its source file has actually changed. See "Auto-cleaning/reload hardening" below.

---

### `_fanout_status` Postgres Table

Created and maintained by `utils/load_data.py:ensure_fanout_status_table()`. Written by `compute_and_write_fanout_status()` after every table load. Read by `add_context` via `_read_fanout_from_metadata()` in `agents/sql_analyst.py`.

```sql
CREATE TABLE _fanout_status (
    table_name   TEXT NOT NULL,
    column_name  TEXT NOT NULL,
    is_likely_fk BOOLEAN NOT NULL,
    has_fanout   BOOLEAN NOT NULL,
    source       TEXT NOT NULL,    -- 'declared_pk', 'declared_fk', 'cardinality_heuristic'
    checked_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (table_name, column_name)
);
```

One row per id-like column per table. `source` indicates how the determination was made:
- `'declared_pk'`: column appears in an `information_schema` PRIMARY KEY constraint — not a FK, no fan-out.
- `'declared_fk'`: column appears in an `information_schema` FOREIGN KEY constraint — is a FK; cardinality checked for `has_fanout`.
- `'cardinality_heuristic'`: no declared constraint; id-like column checked via `COUNT(*)/COUNT(DISTINCT)`. Unique → inferred PK (is_likely_fk=False). Non-unique → inferred FK (is_likely_fk=True, has_fanout=True).

`add_context` reads precomputed rows instead of running live cardinality scans per question. Tables not in `_fanout_status` fall back to live `_detect_fanout_warnings` and log a message to stderr. SELECT granted to `app_reader` by `ensure_fanout_status_table`.

---

## 3. Code Organization

### `agents/sql_analyst.py` — SQL analyst graph nodes and helpers

| Function | Role |
|---|---|
| `curate_question` | Node 1: grammar/spelling fix only, no intent change. Uses `pick_llm("cheap")`. |
| `add_context` | Node 2 (no LLM): builds schema context from `information_schema`, runs fan-out check and DQ status check (including a per-table checksum-freshness re-verification — architecture review point #22), sets `data_quality_action`. |
| `determine_chart_type` | Node (visualization path only): classifies chart type via `ChartTypeSchema` with `pick_llm("cheap")`. |
| `generate_sql` | Node 3: writes one SQL query. Uses `pick_llm("high")`. Extended with chart-shaping instructions on visualization path. **Mechanical compliance retry (architecture review point #25):** after the first LLM call, `_min_sample_rule_violated(state.curated_question, sql_query)` checks whether the minimum-sample-size rule (HAVING COUNT(*) >= 5) should have applied but was silently skipped; if so, the LLM is called exactly once more with the concrete violation and the previous query appended to the prompt (same corrective pattern as a real execution-error retry), and the regenerated query is what's returned. A compliant first attempt makes only one LLM call. |
| `is_safe` | Node 4: deterministic AST safety gate (`_ast_safety_check`, authoritative — architecture review point #23) run first; only if it passes does a `pick_llm("cheap")` / `JudgeSchema` call run as a secondary, non-authoritative sanity check whose comments are surfaced but never override an AST-cleared query back to unsafe. |
| `execute_sql` | Node 6: runs SQL via `app_reader`, sets `SET LOCAL statement_timeout` before every query, caps results at `MAX_RESULT_ROWS = 200`, serializes to JSON via `_ResultEncoder` (Decimal/datetime safe), handles errors and retry counting. Uses `conn.rollback()` (never `conn.commit()`) — read-only connection. **Autocommit guard (architecture review point #26):** `SET LOCAL statement_timeout` only binds within an open transaction — under autocommit=True each `cur.execute()` is its own implicit transaction, so the timeout would never bind to the query that follows it. `get_app_reader_connection()` never sets autocommit (psycopg2 defaults to `False`), but `execute_sql` explicitly checks `conn.autocommit` and forces it off if it's ever `True`, rather than only relying on that default. |
| `cancel_sql` | Node 7: writes final_answer explaining blocked query. |
| `represent_final_answer` | Node 8: plain-English summarization via `pick_llm("cheap")`. Bypassed deterministically for truncated results. |
| `build_visualization` | Visualization terminal node: writes CSV (always), `.hyper` (if Tableau), renders `.png` via `_render_chart_image`, produces final_answer. |
| `_min_sample_rule_violated(question, sql_query)` | Deterministic compliance check (architecture review point #25) for `generate_sql`'s retry above: True only when the question uses ranking/comparison language (`_RANKING_QUESTION_RE`), does NOT say "regardless of size" (`_ALL_GROUPS_REGARDLESS_RE`), the SQL actually GROUPs BY, the aggregation is averaged/rate-based (`_query_has_averaged_or_rate_metric` — AVG(...), or a division where either side is itself COUNT/SUM/AVG), and no HAVING COUNT threshold is present at all (`_extract_min_sample_threshold` returns `None`). Deliberately does not fire for a plain SUM/COUNT total with no ranking intent. |
| `_query_has_group_by(sql_query)` | `tree.find(exp.Group) is not None` — used by `_min_sample_rule_violated`. |
| `_query_has_averaged_or_rate_metric(sql_query)` | True if the AST contains an `exp.Avg`, or an `exp.Div` where either side is `exp.Count`/`exp.Sum`/`exp.Avg`. Used by `_min_sample_rule_violated`. |
| `clean_and_reload` | Auto-clean redirect node. **Fails closed first** (architecture review point #24): checks `sys.stdin.isatty()` before doing anything else (no DB connection opened, no `clean_dataset()` call) — if not interactive, returns a `final_answer` explaining cleaning approval can't be obtained in this context, since this node is reachable automatically from a plain, read-only question and its downstream `_request_approval()` call blocks on a real `input()`. Otherwise: for each targeted table, runs `check_source_freshness()` (logs a `[checksum] source changed, forcing fresh clean for '<table>' (...).` stderr line when the raw source changed since last processed) → calls `clean_dataset()` once per `source_folder` → `load_csv_to_table()` (atomic) → updates `_data_quality_status` (with `source_checksum`) and `_fanout_status`. Accepts `_llm=` for test injection. **Reload coverage is partial by design**: `clean_dataset()` processes every CSV in the folder, but only tables listed in `state.tables_to_clean` (the originally-flagged fail-level ones) get reloaded into Postgres — see the function's docstring and architecture review point #21. |
| `route_after_clean_and_reload` | Conditional edge after `clean_and_reload`: `"add_context"` (normal loop) unless `state.final_answer` is set (the non-interactive fail-closed path), in which case `"end"` → `END` directly. |
| `_find_source_csv(folder_path, table_name, sanitize_identifier)` | Reverse-maps `table_name` back to its raw CSV in `folder_path` by sanitized stem, or `None`. Shared helper used twice in `clean_and_reload` (freshness check + reload loop) to avoid duplicating the reverse-mapping logic. |
| `build_sql_analyst_graph` | Compiles and returns the compiled LangGraph `StateGraph`. |
| `_render_chart_image` | Renders a matplotlib `.png` for the given chart_type. Uses `Figure`/`FigureCanvasAgg` (no global pyplot). Catches all exceptions; returns `None` on failure. |
| `_chart_bar` / `_chart_line` / `_chart_pie` / `_chart_stacked_bar` / `_chart_treemap` | **NULL-safety (P0 architecture review fix):** these used to compute `_to_float(val) or 0`, silently plotting a genuinely missing value as a real zero. Now: `_chart_bar` draws a NULL as a 0-height bar hatched (`"//"`) and annotated "No data", distinct from a real-zero bar (plain, unmarked); `_chart_line` passes NULLs through as `float("nan")`, which matplotlib renders as a real gap in the line (no segment, no marker) — a real zero still plots a marker at y=0; `_chart_pie` and `_chart_treemap` omit a NULL-valued row entirely (a wedge/rectangle has no way to represent "missing" as opposed to a real, invisible zero); `_chart_stacked_bar` hatches only the specific NULL (category, sub-category) cell, stacking it as 0 for the sum but visually marked. `_chart_scatter`/`_chart_histogram`/`_chart_box` already omitted NULL points/values correctly and were unchanged. |
| `_squarify_rects` | Internal treemap layout (no squarify dependency). |
| `_read_fanout_from_metadata(conn, table_names)` | Reads precomputed fan-out data from `_fanout_status`. Returns `(warnings, uncovered_tables)`. Source labels: `'declared_fk'` → "declared foreign key"; `'cardinality_heuristic'` → "inferred from data distribution". |
| `_detect_fanout_warnings(conn, tables, warn_only_for=None)` | Live `COUNT(*)/COUNT(DISTINCT)` fallback. PK/column metadata (`own_pk_columns`/`pk_names`) is gathered across ALL tables passed in (covered and uncovered alike) first; `warn_only_for` only restricts which tables actually get a warning generated, applied AFTER that metadata pass — a covered table's own PK info must be available to the live check for a newly-loaded, uncovered table with a real FK relationship to it (architecture review point #21; fixed a bug where filtering happened before metadata gathering). Warning label: "inferred from data distribution". |
| `_ast_safety_check(sql_query)` | Deterministic, authoritative SQL safety gate for `is_safe` (architecture review point #23). Uses `sqlglot.parse()` (not `parse_one()`, which silently wraps multi-statement input into one `exp.Block` instead of raising) to enforce: exactly one statement, that statement is a SELECT/CTE/set-operation/subquery (never `SELECT ... INTO`), and no write/DDL node (`INSERT`/`UPDATE`/`DELETE`/`DROP`/`ALTER`/`TRUNCATE`/`CREATE`/`MERGE`) anywhere in its tree. Returns `(is_safe, reason)`. |
| `_parse_sql_result` | Parses execute_sql's JSON `{"columns":[...],"rows":[...],"truncated":bool}` string into a list of dicts. Returns `([], False)` for error strings or unparseable input. No eval(), no regex — Decimal/datetime are serialized at source. |
| `_ResultEncoder` | `json.JSONEncoder` subclass used by `execute_sql`: converts `decimal.Decimal` → float, `datetime.datetime`/`datetime.date` → ISO string at serialization time. |
| `_STATEMENT_TIMEOUT_MS` | Module constant (default 30 000 ms). Applied as `SET LOCAL statement_timeout` inside every `execute_sql` call to kill runaway queries at the DB level. |
| `_chart_shaping_instruction` | Returns SQL-shaping instructions per chart type for `generate_sql`'s prompt. |
| `_analyst_judgment_disclosure(sql, result_data=None)` | Unified deterministic disclosure (replaced `_ranking_convention_disclosure`). Covers: HAVING threshold (Rule 1), combined ORDER BY (Rule 2), NULL exclusions (Rule 3), time framing (Rule 4), outlier sensitivity (Rule 5), group size imbalance (Rule 6), SELECT DISTINCT deduplication (Rule 12). Returns `""` when no rule fires. Appended as `"How this answer was computed: ..."` by `represent_final_answer` and `build_visualization`. |
| `_apply_causal_correction(answer, sql)` | Two-pass rewrite: clear causal phrases (`leads to`, `causes`, `because of`, etc.) are replaced in-place with associative equivalents; ambiguous verbs (`drives`, `affects`) trigger the association disclaimer as fallback. Rule 8. |
| `_rubric_applicable_instructions(question)` | Keyword-based: returns extra RUBRIC NOTE strings for `generate_sql`'s human_content when the question implies causal language, per-category averages, or a time scope. Appended to human_content (not system prompt) in `generate_sql`. |
| `_extract_null_exclusion_disclosures(sql)` | Returns one disclosure per `col IS NOT NULL` filter found in the SQL. |
| `_extract_time_framing_disclosure(sql)` | Returns a disclosure when the SQL contains BETWEEN dates, date literals, or date_trunc. |
| `_outlier_sensitivity_note(result_data)` | Fires when one group's metric value is > 3× the median of all groups. |
| `_group_size_imbalance_note(result_data)` | Fires when max/min count-column ratio ≥ 10 in result_data. Requires a count-like column name in the result (n, count, num*, sample_size, total_count, *_count). |
| `_extract_deduplication_disclosure(sql)` | Fires when `SELECT DISTINCT` is present; discloses duplicate-removal assumption. Rule 12. |
| `_parse_sql_ast(sql_query)` | Parses `sql_query` into a `sqlglot` AST (`read="postgres"`), memoized per query text in `_SQL_AST_CACHE`. Returns `None` on empty input or a parse failure — every caller below treats that as "nothing extractable," not an error. |
| `_extract_referenced_tables(sql_query)` | AST-based table-reference extraction: walks all `exp.Table` nodes (including inside subqueries and CTE bodies), returns lowercase names, excluding CTE names themselves (a CTE re-selected via `FROM cte_name` would otherwise look like a table). Replaces the old regex substring match — a table name inside a string literal or comment no longer matches. |
| `_query_touches_table(sql_query, table_name)` | Thin wrapper: `table_name.lower() in _extract_referenced_tables(sql_query)`. |
| `_extract_min_sample_threshold(sql_query)` | AST-based HAVING extraction (Rule 1): finds every `exp.Having` clause, walks for a `GTE`/`GT` comparison whose left side is `exp.Count` and right side is a numeric `exp.Literal`, returns that literal. Catches `HAVING COUNT(*) >= 5`, `HAVING COUNT(order_id) >= 5`, `HAVING COUNT(*) > 4`, and any structurally equivalent form — not one fixed regex shape. Searches all HAVING clauses in the query (including inside CTEs), matching the old regex's whole-text search behavior. |
| `_extract_order_by_columns(sql_query)` | AST-based ORDER BY extraction (Rule 2): reads the *outer* query's own `order` arg directly (not `tree.find(exp.Order)`, which could return a CTE's own ORDER BY instead). Returns `[(label, direction), ...]`. A plain column key is humanized via `_humanize_column`; a CASE expression, window function, or other compound expression has no single "column" to name, so its real SQL text is shown verbatim instead of a guessed label. |
| `_order_by_label(order_expr)` | Label helper used by `_extract_order_by_columns` — `exp.Column` → humanized name; everything else → `expr.sql(dialect="postgres")`. |

**SQL analyst graph wiring:**
```
START → curate_question → add_context
add_context →(route_after_add_context)→ generate_sql | determine_chart_type | clean_and_reload
clean_and_reload →(route_after_clean_and_reload)→ add_context (loops; stop via
    cleaning_attempted_tables) | END (fail-closed: no interactive stdin)
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
| `router_node` | Classifies message via `RouterSchema` / `pick_llm("cheap")`. |
| `sql_node` | Invokes `_SQL_ANALYST_GRAPH` with `wants_visualization=False`. Returns the full sub-agent result dict as `sql_analyst_trace` on its own node output (architecture review point #27) — no module-level global; see `DataAgentSchema.sql_analyst_trace` above. |
| `visualize_node` | Invokes `_SQL_ANALYST_GRAPH` with `wants_visualization=True`. Returns `sql_analyst_trace` exactly as `sql_node` does. |
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
| `extract_load` | `@tool`: HTTP GET download to a local folder. Never authenticates. **SSRF-protected (architecture review point #28, permanent safety boundary — see AGENTS.md):** calls `_validate_fetch_url` before the initial fetch AND before following any redirect (redirects are followed manually via `allow_redirects=False`, not automatically, capped at `_MAX_REDIRECTS = 5`), so a URL that passes validation but redirects to an internal address is still blocked. |
| `_validate_fetch_url(url)` | Resolves the URL's hostname to its real, current IP address(es) (`socket.getaddrinfo`) and rejects if any is private/loopback/link-local/reserved/multicast/unspecified (`ipaddress.ip_address(...).is_private` etc. — this is what blocks `169.254.169.254`, the AWS/GCP/Azure metadata endpoint, since it's link-local) or the hostname is a known metadata name (`metadata.google.internal`, `metadata.goog`). Only `http`/`https` schemes allowed. Returns `(is_safe, reason)`. |
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
| `_clone_file(file_path, cleaned_dir)` | Copies `file_path` into `cleaned_dir` as `<file>`. Bounded two-generation versioning (architecture review point #20): if `<file>` already exists from a prior cleaning run, it's rotated to `<file>.previous` first — one generation of history, not unlimited. Only called for files with real issues (untouched files get no clone at all). |
| `_describe_file_for_prompt(file_path, df=None)` | Builds sample-rows + column context block for the code-gen prompt. Accepts optional pre-loaded `df` to avoid reading the CSV twice. |
| `_issue_guidance(issue, df=None)` | Appends deterministic guidance under a single issue line. For "Missing values" issues under the 20% ceiling, also calls `_categorical_fill_advice` when `df` is provided to recommend mode vs. `'Unknown'`. |
| `_categorical_fill_advice(df, col)` | Returns a fill-value recommendation for a categorical column: mode when top category ≥ 40% share, `'Unknown'` when distribution is roughly even. Returns `""` for numeric or high-cardinality columns. |
| `_CATEGORICAL_DOMINANT_THRESHOLD` | `0.40` — top-category share at or above this → mode is a safe fill value; below → `'Unknown'`. |

**`FAIL_LEVEL_PREFIXES`** (issues that produce `status="fail"` in `_data_quality_status`):
`"Duplicate rows:"`, `"Duplicate values:"`, `"Wrong data type:"`, `"Invalid values:"`, `"Encoding problem:"`, `"Structural issue:"`, `"Placeholder values:"`, `"Lost leading zeros:"`, `"Locale-specific number formatting:"`, `"Spreadsheet artifacts:"`, `"Column misalignment:"`, `"Header row duplicated mid-file:"`, `"Byte-order-mark:"`, `"Dangling references:"`

**`WARN_LEVEL_PREFIXES`** (issues that produce `status="warn"`):
`"Missing values:"`, `"Inconsistent categorical values:"`, `"Formatting noise:"`, `"Inconsistent boolean representations:"`, `"Currency/unit symbols:"`, `"Excessive floating-point noise:"`, `"Non-printable characters:"`, `"Inconsistent delimiters:"`, `"Column header issues:"`, `"Trailing empty rows:"`, `"Trailing empty column:"`, `"Special characters in headers:"`, `"Inconsistent granularity:"`

---

### `utils/load_data.py` — Admin-only data loader

| Function | Role |
|---|---|
| `get_admin_connection()` | Opens psycopg2 connection as admin/superuser. **Only file that uses this.** No password needed (local trust auth). Env vars: `PG_HOST`, `PG_PORT`, `PG_DATABASE`, `PG_ADMIN_USER`. |
| `ensure_data_quality_status_table(conn)` | Creates `_data_quality_status` if missing; runs `ADD COLUMN IF NOT EXISTS source_folder` and `ADD COLUMN IF NOT EXISTS source_checksum` migrations idempotently. |
| `ensure_fanout_status_table(conn)` | Creates `_fanout_status` if missing; GRANTs SELECT to `app_reader`. Called by `main()` and `clean_and_reload`. |
| `compute_and_write_fanout_status(conn, table_name)` | Computes fan-out metadata for all id-like columns in `table_name` (using declared PK/FK constraints first, cardinality heuristic as fallback) and writes rows to `_fanout_status`. DELETEs old rows first so a reload always reflects current data. Called after every `load_csv_to_table`. |
| `_is_id_like_column(col)` | Returns True for `'id'` or any column ending in `'_id'`. Used by `compute_and_write_fanout_status`. |
| `compute_quality_status(unresolved_issues)` | Returns `(status, issues_found_payload)` using `_issue_severity` from `data_cleaning.py`. Status: `"fail"` > `"warn"` > `"pass"`. |
| `write_data_quality_status(conn, table_name, status, issues_found, was_cleaned, source_folder=None, source_checksum=None)` | Upserts one row in `_data_quality_status`. `source_checksum` is the raw source file's SHA-256 at processing time (see below); `None` means "no baseline". |
| `compute_file_checksum(path)` | Real SHA-256 hex digest of `path`'s bytes, streamed in 1 MiB chunks. |
| `get_stored_checksum(conn, table_name)` | Returns the `source_checksum` last recorded for `table_name`, or `None` if there's no row yet or the column is `NULL`. Commits immediately after the SELECT to release its lock (same pattern as `_fetch_status` in `tests/test_auto_clean_redirect.py`). |
| `check_source_freshness(conn, table_name, csv_path)` | Returns `(source_changed, current_checksum)`. `source_changed` is `True` only when a prior checksum exists AND differs from the file's current bytes — a table with no baseline yet is never reported as "changed". Called by `add_context` (real gate for non-`"fail"`-status tables — architecture review point #22, see the `_data_quality_status` section above), `clean_and_reload` (per targeted table, informational only — those tables are already being reloaded regardless of this result), and `load_data.py:main()` (per file, informational only). All three log `[checksum] source changed, forcing fresh clean for '<table>' (...).` to stderr/stdout when a mismatch is detected — see architecture review points #20/#22. |
| `_create_and_populate_table(conn, table_name, csv_path, sample_rows_for_typing=500)` | Low-level create+populate primitive (CREATE TABLE + batched INSERT from the CSV). Raises on any failure (malformed row, type mismatch); never commits — callers own the transaction. Used internally by `load_csv_to_table` to build the staging table. |
| `_swap_table_atomically(conn, staging_name, table_name)` | Atomic swap: any existing `table_name` is renamed to `table_name_previous` (kept, never dropped) before `staging_name` is renamed to `table_name`. Runs inside the caller's still-open transaction — no commit here. `ALTER TABLE ... RENAME` is atomic DDL in Postgres, so `table_name` is never observably missing or half-populated. |
| `load_csv_to_table(conn, csv_path, sample_rows_for_typing=500)` | Loads a CSV into Postgres **atomically** (architecture review point #20): builds a staging table (`_create_and_populate_table`) inside one transaction, then swaps it into place (`_swap_table_atomically`); a single `conn.commit()` at the end covers both steps. Any exception during staging triggers `conn.rollback()` and re-raises — the existing `table_name` (if any) is never touched. Staging table name is deterministic (`__reload_staging__<table_name>`, not random), so an orphaned staging table from a crashed prior load self-heals via the `DROP TABLE IF EXISTS` at the start of `_create_and_populate_table`. Returns `(table_name, row_count)` — same contract as before. |
| `rollback_table(conn, table_name)` | **Manual, deliberate** recovery: swaps `table_name` and `table_name_previous` back (a real swap via a temporary holding name, not a destructive overwrite — the bad version becomes the new `_previous`). Returns `True` if a rollback happened, `False` if there was no `_previous` to roll back to. Nothing in this codebase calls this automatically. |
| `sanitize_identifier(name)` | Lowercases and strips a CSV stem to make a valid Postgres identifier (used as table name). |

---

### `utils/db.py` — Read-only connection helper

`get_app_reader_connection()`: connects as `PG_APP_READER_USER` with `PG_APP_READER_PASSWORD`. Used by every graph node that reads data (`add_context`, `execute_sql`, `_detect_fanout_warnings`, and all report helpers). Env vars loaded from `~/.hermes/profiles/data-agent/.env`.

---

### `utils/generate_presentation.py` — HTML slideshow generator

Public API: `generate_presentation(entry: dict) -> str` (returns path to written `.html`).

Writes vanilla-JS slideshows to `presentations/<slug>_<timestamp>.html`. Slide set is determined by `route_response` in the log entry:
- Always present: Title, "What are we exploring?", Summary ("Key Takeaways")
- sql_analyst entries: Question slide added
- Cleaning history present: Uncleaned Data + Issue/Solution + Cleaned Data slides (omitted entirely when no cleaning history exists for the queried tables)
- visualize entries: Visualization slide with chart embedded as base64 `<img>`; "Why this chart type" section added when `chart_type_source == "reasoned"`

JS navigation: `show(n)`, ArrowRight/ArrowLeft keyboard support, `show(0)` on load. HTML attribute IDs use double quotes (`id="prev"`, `id="next"`, `id="counter"`).

**CLI triggers** (in `main.py`):
- `python main.py "present last"` — builds slideshow from most recent `query_log.jsonl` entry without re-running the query.
- `python main.py "present: <question>"` — runs the question fresh, then builds a slideshow from that run's log entry.

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
- `app_reader` — DB-enforced read-only (SELECT + USAGE only; INSERT/UPDATE/DELETE/TRUNCATE/CREATE revoked at the database level — see AGENTS.md invariant). All graph runtime queries use this role (`utils/db.py:get_app_reader_connection()`). Never re-add write grants — the LLM safety judge is a UX layer, not the security boundary.
- Admin/superuser — write access. Used exclusively by `utils/load_data.py:get_admin_connection()`. Also used by `clean_and_reload` node (which calls `load_data.py` functions).

**Severity mapping location:** `utils/data_cleaning.py` — `FAIL_LEVEL_PREFIXES` and `WARN_LEVEL_PREFIXES` tuples at module level (lines 177–208). `compute_quality_status` in `utils/load_data.py` calls `_issue_severity` imported from `data_cleaning.py` — same mapping, one source of truth.

**Output directories:**
- `outputs/visualizations/` — CSV and PNG files from `build_visualization`. Naming: `{40-char-slug}_{YYYYMMDD_HHMMSS}.{csv,png}`. Tableau also writes a `.hyper` file alongside the CSV.
- `reports/` — HTML reports from `generate_report`. Naming: `{40-char-slug}_{YYYYMMDD_HHMMSS}.html`.
- `presentations/` — HTML slideshows from `generate_presentation`. Same naming convention.
- `logs/` — `query_log.jsonl` and `cleaning_log.jsonl`.

**Analyst judgment rubric:** `analyst-judgment-rubric.md` at the project root documents all 13 rules enforced by `GENERATE_SQL_SYSTEM_PROMPT` and the disclosure helpers. Rules 1–8 and 12 have post-execution disclosure via `_analyst_judgment_disclosure`; Rules 9–11 and 13 are prompt-only enforcement.

---

## 5. Testing Conventions

**File naming:** `tests/test_<feature>.py` — standalone executable scripts, not pytest. Run with `PYTHONPATH=/path/to/data-agent uv run python tests/test_<feature>.py`.

**`_llm=None` / `llm=None` injection pattern:** Both `clean_dataset(folder_path, llm=None, ...)` and `clean_and_reload(state, _llm=None)` accept a fake LLM object in tests. When `None` (always in production), they call `pick_llm("high")`. Tests pass deterministic fake LLMs (e.g. `FakeDedupLLM`, `AlwaysFailLLM`, `AnyCodeLLM`) to control code generation without live API calls. Used in: `test_auto_clean_redirect.py`, `test_data_cleaning_retry.py`, `test_data_cleaning_post_validation.py`, `test_data_cleaning_fail_warn_split.py`, `test_data_quality_status.py`, `test_visualize_auto_clean.py`, `test_reload_hardening.py`.

**Data fixtures:** Test data lives under `data/_test_etl/` — separate subdirectory per scenario (`clean_only/`, `dq_fail/`, `dq_warn/`, `approve_yes/`, `reload_hardening/`, `checksum_gate/`, etc.). Tests that require interactive approval (`test_data_cleaning_approve.py`) require stdin to be piped — they fail with `EOFError` when run non-interactively; this is expected behavior, not a regression.

**Simulating an interactive session for `clean_and_reload` (architecture review point #24):** `clean_and_reload` checks `sys.stdin.isatty()` before doing anything else and fails closed if it's `False` — but a piped `printf 'yes\n' | ...` stdin (used throughout this suite to script approval answers) is never a tty either, even though `input()` reads the piped lines successfully. Any test that calls `clean_and_reload` directly and needs it to actually proceed (not fail closed) must wrap the call in `with patch("sys.stdin.isatty", return_value=True):` (from `unittest.mock`) — see `test_auto_clean_redirect.py`'s `approve`/`decline` scenarios, `test_reload_hardening.py`'s `clean` mode, and `test_visualize_auto_clean.py`. This check lives in `clean_and_reload` itself, not in `utils/data_cleaning.py:_request_approval` — that function is shared by manual CLI cleaning (`clean_data.py`, `utils/load_data.py`) and most of the test suite, both of which legitimately pipe answers to a non-tty stdin and must NOT be gated.

**Mode-based test files:** A few test files take a CLI arg to select which scenario(s) to run, because different scenarios need different (or no) piped stdin — see each file's own docstring for the exact `printf` invocation per mode:
- `test_auto_clean_redirect.py`: `routing` (no stdin) / `decline` (`no\n`) / `approve` (`yes\nyes\n`) / `noninteractive` (no stdin — architecture review point #24: `clean_and_reload` fails closed with a clear `final_answer` when `sys.stdin.isatty()` is forced `False`, and `route_after_clean_and_reload` routes straight to `END`).
- `test_reload_hardening.py`: `db` (no stdin — atomic swap/rollback, forced-failure, checksum-unit scenarios) / `clean` (`yes\n` × 4 — cleaned-artifact versioning + `clean_and_reload` checksum-logging integration scenarios). Covers architecture review points #20/#21 (Tier 4 auto-clean/reload hardening): atomic `load_csv_to_table`, `rollback_table`, `check_source_freshness`, and `_clone_file`'s `.previous` rotation. Fixture: `data/_test_etl/reload_hardening/reload_dup.csv`.

**Other relevant single-purpose tests:**
- `test_fanout_status.py`: scenarios 6-7 cover the partial-coverage fan-out fix (architecture review point #21) — scenario 6 is the real end-to-end DB check, scenario 7 uses a fake connection that logs every query `_detect_fanout_warnings` issues, to directly prove metadata gathering now queries covered tables too (id-suffixed column names alone already satisfy `_is_id_like_column`, so scenario 6 alone doesn't distinguish pre-/post-fix behavior — scenario 7 does).
- `test_is_safe.py`: includes the deterministic AST-gate assertions (architecture review point #23) — a semicolon-chained multi-statement injection rejected outright with `pick_llm` monkeypatched to raise if ever called (proving the LLM is never consulted), plus `SELECT ... INTO`, a CTE, and a comment-only fake "second statement" as negative/positive controls on `_ast_safety_check` directly.
- `test_checksum_forces_reclean.py`: DB-only, no LLM/stdin — proves a `"pass"`-status table whose source file changes on disk is forced through `needs_cleaning`/`tables_to_clean` by `add_context` (architecture review point #22), and that an unchanged source doesn't regress the existing "proceed" behavior.
- `test_chart_null_handling.py`: unit-level (no LLM/DB) — calls `_chart_bar`/`_chart_line`/`_chart_pie`/`_chart_stacked_bar`/`_chart_treemap` directly with a real matplotlib Axes/Figure and a dataset containing one NULL metric value; asserts the NULL is hatched/annotated/NaN-gapped/omitted as appropriate and never rendered identically to a real zero (P0 architecture review fix).
- `test_statement_timeout.py`: STEP 0 opens a real `get_app_reader_connection()` and asserts `conn.autocommit is False` before trusting the timeout-kill assertion as proof under the connection's real configuration (architecture review point #26); STEP 3 forces a connection into `autocommit=True` via a monkeypatched `get_app_reader_connection` and confirms `execute_sql`'s own defensive guard still makes the timeout apply.
- `test_router_state_no_global.py`: asserts `agents.router` no longer has a `LAST_SQL_ANALYST_STATE` attribute, then runs two `sql_node` calls in separate real threads (one deliberately slower, via a fake `_SQL_ANALYST_GRAPH`) and confirms each call's returned `sql_analyst_trace` holds only its own data even though the faster thread's call completes first (architecture review point #27).
- `test_extract_load_ssrf.py`: unit tests `_validate_fetch_url` against metadata IPs/hostnames, private ranges, loopback, and a non-http(s) scheme, plus a real public URL (must still pass); then confirms `extract_load` itself refuses a metadata-endpoint URL with no output folder ever created (rejected before any request/I/O), while a real public download still succeeds (architecture review point #28).
- `test_min_sample_rule_compliance.py`: unit tests `_min_sample_rule_violated` across the six firing/non-firing cases; then monkeypatches `pick_llm` (same pattern as `test_is_safe.py`) with a fake LLM returning a non-compliant query on its first call and a compliant one on its second, confirming `generate_sql` makes exactly 2 calls and returns the corrected query — and confirms a compliant first attempt makes exactly 1 call, no wasted retry (architecture review point #25).

**`PYTHONPATH` requirement:** All tests must be run from the project root with `PYTHONPATH=/Users/dylangagliordi/data-agent` set (or equivalent), since `agents/`, `models/`, and `utils/` are not installed packages.
