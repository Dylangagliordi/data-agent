"""Spec 2 (Final) — one shared, ordered narrative walkthrough that both
report.py and presentation.py render from, so the two documents always agree
in sequence and content and differ only in HTML-vs-slide formatting.

Public interface:
    build_narrative_walkthrough(entry: dict) -> list[NarrativeStep]
    narrate_steps(steps: list[NarrativeStep], llm) -> list[NarrativeStep]

build_narrative_walkthrough is a PURE, deterministic assembly of real facts
only (cleaning_log.jsonl, the live DB schema, entry["transformation_narrative_log"]
/ entry["transformation_candidates_not_relevant"] written by
agents/sql_analyst.py:surface_transformations, and
utils/sql_transform_extraction.py) — no LLM call, so it is cheap to call
repeatedly and trivially testable without network access. Every explanation
is already real, readable prose even before narration; narrate_steps is a
SEPARATE, optional rewrite pass (one combined LLM call, called once per
document by generate_report.py / generate_presentation.py) that turns that
prose into first-person, "student explaining their own process" language,
never inventing a new fact.

Never invents a step that didn't happen; never omits a step that did. The
Part-B-specific rule (never invent a reason for a transformation decision
that has no logged reasoning) is enforced twice: the deterministic
explanation built here already states plainly when no reason beyond the
choice itself was recorded, and NARRATIVE_WALKTHROUGH_PROMPT repeats the
same instruction to the LLM narration pass.
"""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CLEANING_LOG_PATH = _PROJECT_ROOT / "logs" / "cleaning_log.jsonl"

_COMPOSITE_FIELD_PREFIX = "Composite field (discovered):"
_DUPLICATE_ROWS_PREFIX = "Duplicate rows:"

_COLUMN_RE = re.compile(r"column '([^']+)'")
_ACROSS_LIST_RE = re.compile(r"across \[(.*?)\]")
_COMPOSITE_RE = re.compile(
    r"column '([^']+)' has (\d+) value\(s\) \((\d+)%\) matching the pattern '(.*)' —"
)
_DUP_COUNT_RE = re.compile(r"Duplicate rows: (\d+) fully duplicate rows found")


@dataclass
class NarrativeStep:
    step_number: int
    part: str                  # "cleaning" | "transformation" | "analysis" | "conductor" (Spec 16)
    title: str                 # short, e.g. "Split Headquarters into city and country"
    explanation: str           # plain-English, first-person-explainable prose
    technical_detail: str = "" # optional: real formula/regex/threshold, verbatim
    stats: dict = field(default_factory=dict)  # real before/after numbers, if any


def build_conductor_narrative(goal: str, tool_calls: list) -> list:
    """Spec 16, Part 2: pure, deterministic assembly (no LLM — same discipline
    as build_narrative_walkthrough) turning a real Conductor run
    (agents.conductor.run_conductor's own real, ordered "tool_calls" —
    [{"tool", "input", "output"}, ...]) into the SAME NarrativeStep shape
    generate_report/generate_presentation/notebook_export already render
    unchanged.

    Every step gets part="conductor" — a new part value. The existing
    renderers already fall back gracefully for any part they have no
    special-cased label for (_PART_TITLES.get(part, part.title()) in
    generate_report.py, _PART_LABELS.get(part, part.title()) in
    generate_presentation.py), rendering a plain "Conductor" section header —
    no changes needed to either renderer for this to work.

    Never invents what a tool call "meant" — explanation states only the
    real tool name, the real input it was given, and the real output it
    returned, verbatim. A run with zero tool calls (the goal was answered
    with no investigation at all) produces a single honest step saying so,
    never a fabricated investigation.
    """
    steps = [
        NarrativeStep(
            step_number=1,
            part="conductor",
            title="Goal",
            explanation=f"The stated goal was: {goal}",
        )
    ]
    if not tool_calls:
        steps.append(
            NarrativeStep(
                step_number=2,
                part="conductor",
                title="No investigation needed",
                explanation="The goal was answered directly, with no tool calls made.",
            )
        )
        return steps

    for i, call in enumerate(tool_calls, start=2):
        steps.append(
            NarrativeStep(
                step_number=i,
                part="conductor",
                title=f"Called {call['tool']}",
                explanation=(
                    f"Called the real `{call['tool']}` tool with input {call['input']!r}. "
                    f"Real result: {call['output']}"
                ),
                technical_detail=f"Input: {call['input']}\n\nOutput: {call['output']}",
            )
        )
    return steps


# ── DB / table lookups (own copy — same convention already used independently
# by utils/generate_report.py and utils/generate_presentation.py) ──────────

def _all_table_names() -> list:
    from utils.db import get_app_reader_connection
    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = %s ORDER BY table_name",
                ("public",),
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def _detect_tables_in_sql(sql: str, known_tables: list) -> list:
    sql_upper = sql.upper()
    return [t for t in known_tables if re.search(r"\b" + re.escape(t.upper()) + r"\b", sql_upper)]


def _table_metadata(table_names: list) -> list:
    if not table_names:
        return []
    from utils.db import get_app_reader_connection
    conn = get_app_reader_connection()
    meta = []
    try:
        with conn.cursor() as cur:
            for tname in table_names:
                cur.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
                    ("public", tname),
                )
                columns = [{"name": row[0], "type": row[1]} for row in cur.fetchall()]
                cur.execute(f'SELECT COUNT(*) FROM "{tname}"')  # noqa: S608 — tname from information_schema
                row_count = cur.fetchone()[0]
                meta.append({"table": tname, "row_count": row_count, "columns": columns})
    finally:
        conn.close()
    return meta


def _cleaning_entries_for_tables(table_names: list) -> dict:
    """Most recent cleaning_log.jsonl (entry_meta, file_rec) pair per table_name."""
    result = {t: None for t in table_names}
    if not _CLEANING_LOG_PATH.exists() or not table_names:
        return result
    normalized_lookup = {t.lower().strip(): t for t in table_names}
    for line in _CLEANING_LOG_PATH.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        for file_rec in entry.get("files", []):
            tname = file_rec.get("table_name", "").lower().strip()
            if tname in normalized_lookup:
                result[normalized_lookup[tname]] = (entry, file_rec)
    return result


def _parse_result_rows(result_str: str) -> "tuple[list, bool]":
    if not result_str or result_str.startswith("SQL_EXECUTION_ERROR"):
        return [], False
    try:
        payload = json.loads(result_str)
    except (json.JSONDecodeError, TypeError):
        return [], False
    if not isinstance(payload, dict):
        return [], False
    columns = payload.get("columns") or []
    rows = payload.get("rows") or []
    truncated = bool(payload.get("truncated", False))
    if not columns or not isinstance(rows, list):
        return [], truncated
    return [dict(zip(columns, r)) for r in rows], truncated


# ── Small text helpers ─────────────────────────────────────────────────────

def _clean_reasoning(reasoning: list) -> str:
    if not reasoning:
        return ""
    return " ".join(re.sub(r"^#\s*", "", c) for c in reasoning).strip()


def _issue_title(issue_text: str) -> str:
    prefix, _, _rest = issue_text.partition(":")
    return prefix.strip() or "Data issue"


def _fmt_payload(payload) -> str:
    if isinstance(payload, (list, tuple, set, frozenset)):
        return ", ".join(f"'{p}'" for p in sorted(payload))
    return str(payload)


_SIGNATURE_DESCRIPTIONS = {
    "placeholder": lambda p: f"they all use the same placeholder value(s) {_fmt_payload(p)} standing in for real missing data",
    "currency_unit": lambda p: f"they all had the same currency/unit symbol(s) {_fmt_payload(p)} mixed into what should be plain numbers",
    "spreadsheet_artifact": lambda p: f"they all show the same kind of spreadsheet artifact ({p})",
    "locale_format": lambda p: f"they all mixed the same two number-formatting locales ({_fmt_payload(p)})",
    "boolean_families": lambda p: f"they all used the same inconsistent boolean representations ({_fmt_payload(p)})",
    "leading_zeros": lambda p: f"they all lost leading zeros down to the same fixed length ({p})",
}


def _describe_signature(signature) -> str:
    if not signature:
        return "they all needed the identical fix"
    kind = signature[0]
    payload = signature[1] if len(signature) > 1 else None
    dtype = signature[2] if len(signature) > 2 else None
    desc_fn = _SIGNATURE_DESCRIPTIONS.get(kind)
    base = desc_fn(payload) if desc_fn else "they shared the exact same underlying issue"
    if dtype:
        base += f" (all {dtype} columns)"
    return base


def _diff_composite_split_columns(source_folder: str, file_name: str, flagged_column: str) -> "list | None":
    """Best-effort attribution of the two real new column names a resolved
    composite-field split produced, by diffing the current cleaned CSV
    against its one kept previous generation (utils/data_cleaning.py's
    _clone_file versioning). Only returns real names when the diff cleanly
    isolates to exactly this split (originally-flagged column gone, exactly
    two new ones) — if other columns changed too (a different fix touched
    the same file in the same run), returns None rather than guessing which
    of several new columns belongs to which fix.
    """
    try:
        import pandas as pd

        cleaned_dir = Path(source_folder) / "cleaned"
        current_path = cleaned_dir / file_name
        previous_path = cleaned_dir / f"{file_name}.previous"
        if not current_path.exists() or not previous_path.exists():
            return None
        current_cols = list(pd.read_csv(current_path, nrows=0).columns)
        previous_cols = list(pd.read_csv(previous_path, nrows=0).columns)
        removed = [c for c in previous_cols if c not in current_cols]
        added = [c for c in current_cols if c not in previous_cols]
        if removed == [flagged_column] and len(added) == 2:
            return added
        return None
    except Exception:
        return None


# ── Part A: Data Cleaning step builders ─────────────────────────────────────

def _add_singleton_fail_step(add, tname, issue_text, detail, resolved_set):
    is_resolved = issue_text in resolved_set
    reasoning_text = _clean_reasoning(detail.get("reasoning_comments", []))
    title = f"{_issue_title(issue_text)} — {tname}"
    explanation = f"I found this issue in {tname}: {issue_text} "
    explanation += "This was fixed." if is_resolved else "This was NOT resolved — it remains in the data."
    if reasoning_text:
        explanation += f" How it was fixed: {reasoning_text}"
    add(
        "cleaning", title, explanation, technical_detail=issue_text,
        stats={"table": tname, "resolved": is_resolved},
    )


def _extract_batch_columns(issues: list) -> list:
    """Every real column an issue's own text names — the plain `column
    '<name>'` form most checks use, plus the `across [...]` bracketed-list
    form a few whole-file checks (e.g. header-casing) use instead. Order
    preserved, de-duplicated.
    """
    cols: list = []
    seen: set = set()
    for issue_text in issues:
        m = _COLUMN_RE.search(issue_text)
        if m:
            if m.group(1) not in seen:
                seen.add(m.group(1))
                cols.append(m.group(1))
            continue
        am = _ACROSS_LIST_RE.search(issue_text)
        if am:
            for c in re.findall(r"'([^']*)'", am.group(1)):
                if c not in seen:
                    seen.add(c)
                    cols.append(c)
    return cols


def _add_batch_step(add, tname, batch, level="fail"):
    issues = batch.get("issues", [])
    cols = _extract_batch_columns(issues)
    signature = batch.get("signature")
    reason_phrase = _describe_signature(signature)
    is_resolved = batch.get("status") == "resolved"
    reasoning_text = _clean_reasoning(batch.get("reasoning_comments", []))
    col_list = ", ".join(cols) if cols else f"{len(issues)} columns"
    level_word = "critical" if level == "fail" else "advisory"
    n_cols = len(cols) or len(issues)
    col_word = "column" if n_cols == 1 else "columns"
    title = f"Fix the same {level_word} issue across {n_cols} {col_word} — {tname}"
    explanation = (
        f"In {tname}, these columns needed the exact same fix: {col_list}. "
        f"I treated them together in one pass because {reason_phrase}. "
    )
    explanation += "This was fixed." if is_resolved else "This was NOT fully resolved."
    if reasoning_text:
        explanation += f" How it was fixed: {reasoning_text}"
    add(
        "cleaning", title, explanation,
        technical_detail=json.dumps({"signature": signature, "issues": issues}, default=str),
        stats={"table": tname, "columns": cols, "resolved": is_resolved, "batched": True},
    )


def _add_composite_step(add, tname, issue_text, detail, resolved_set, entry_meta, file_rec):
    is_resolved = issue_text in resolved_set
    m = _COMPOSITE_RE.search(issue_text)
    column = m.group(1) if m else ""
    match_count = m.group(2) if m else ""
    match_pct = m.group(3) if m else ""
    pattern = m.group(4) if m else ""
    reasoning_text = _clean_reasoning(detail.get("reasoning_comments", []))

    new_cols = None
    if is_resolved and column:
        source_folder = entry_meta.get("source_folder", "")
        file_name = file_rec.get("file_name", "")
        if source_folder and file_name:
            new_cols = _diff_composite_split_columns(source_folder, file_name, column)

    title = f"Split {column} into two columns — {tname}" if column else f"Split a composite field — {tname}"
    explanation = (
        f"While looking at {tname}, I noticed the '{column}' column actually holds two "
        f"different pieces of information glued together — {match_pct}% of its real values "
        f"({match_count} rows) matched the pattern {pattern!r}. "
        "This wasn't caught by a fixed rule — it came from the pipeline's exploratory "
        "discovery pass, which doesn't check every column uniformly (it skips columns "
        "already flagged for another issue, long prose columns, and sequential ID columns), "
        "so a similarly composite column elsewhere could be missed if it happened to be "
        "flagged for something else first."
    )
    if is_resolved:
        if new_cols:
            explanation += (
                f" I split it into two new columns — {new_cols[0]} and {new_cols[1]} — "
                "and dropped the original."
            )
        else:
            explanation += (
                " I split it into two new columns and dropped the original (the exact new "
                "column names aren't independently verifiable from the log for this run, "
                "since other fixes also touched this file in the same cleaning pass)."
            )
    else:
        explanation += " This was not resolved."
    if reasoning_text:
        explanation += f" {reasoning_text}"
    add(
        "cleaning", title, explanation, technical_detail=issue_text,
        stats={"table": tname, "column": column, "match_pct": match_pct, "resolved": is_resolved, "new_columns": new_cols},
    )


def _add_duplicate_step(add, tname, dup_issue, resolved_set):
    if dup_issue is None:
        add(
            "cleaning", f"Duplicate-row check — {tname}",
            f"I checked {tname} for fully duplicate rows and found none.",
            stats={"table": tname, "duplicates_found": 0},
        )
        return
    issue_text = dup_issue.get("issue", "")
    m = _DUP_COUNT_RE.search(issue_text)
    count = m.group(1) if m else "some"
    is_resolved = issue_text in resolved_set
    explanation = f"I checked {tname} for fully duplicate rows and found {count}."
    explanation += " These were removed." if is_resolved else " They were not removed — they remain in the table."
    add(
        "cleaning", f"Duplicate-row check — {tname}", explanation, technical_detail=issue_text,
        stats={"table": tname, "duplicates_found": count, "resolved": is_resolved},
    )


def _add_singleton_warn_step(add, tname, issue_text, resolved_set):
    is_resolved = issue_text in resolved_set
    title = f"{_issue_title(issue_text)} — {tname}"
    explanation = f"I also found this advisory issue in {tname}: {issue_text} "
    explanation += "This was fixed." if is_resolved else "This was left as-is (advisory, not required for correctness)."
    add(
        "cleaning", title, explanation, technical_detail=issue_text,
        stats={"table": tname, "resolved": is_resolved},
    )


def _build_cleaning_steps(add, table_meta: list, cleaning_map: dict) -> None:
    for tm in table_meta:
        tname = tm["table"]
        add(
            "cleaning", f"Load {tname}",
            f"I started by loading the {tname} table, which has {tm['row_count']:,} rows "
            f"and {len(tm['columns'])} columns.",
            stats={"table": tname, "row_count": tm["row_count"], "column_count": len(tm["columns"])},
        )

        rec = cleaning_map.get(tname)
        if rec is None:
            add(
                "cleaning", f"No cleaning history — {tname}",
                f"{tname} has no recorded cleaning history in this system — it has never "
                "been processed through the cleaning pipeline, so data quality has not "
                "been verified for it.",
                stats={"table": tname},
            )
            continue

        entry_meta, file_rec = rec
        issues_found = file_rec.get("issues_found", [])
        fail_issues_found = [
            i for i in issues_found
            if i.get("severity") == "fail" and not i.get("issue", "").startswith(_DUPLICATE_ROWS_PREFIX)
        ]
        warn_issues_found = [i for i in issues_found if i.get("severity") == "warn"]
        dup_issue = next(
            (i for i in issues_found if i.get("issue", "").startswith(_DUPLICATE_ROWS_PREFIX)), None
        )

        fail_detail_map = {fi["issue"]: fi for fi in file_rec.get("fail_issues", [])}
        fail_batch_by_issue = {}
        for batch in file_rec.get("fail_batches", []):
            for bi in batch.get("issues", []):
                fail_batch_by_issue[bi] = batch
        resolved_set = set(file_rec.get("issues_resolved", []))
        rendered_fail_batches = set()

        if not issues_found:
            add(
                "cleaning", f"No issues found — {tname}",
                f"{tname} was scanned for data quality issues and none were found — the "
                "data loaded cleanly.",
                stats={"table": tname},
            )
            continue

        for iss in fail_issues_found:
            issue_text = iss.get("issue", "")
            batch = fail_batch_by_issue.get(issue_text)
            if batch is not None:
                if id(batch) in rendered_fail_batches:
                    continue
                rendered_fail_batches.add(id(batch))
                _add_batch_step(add, tname, batch, level="fail")
            elif issue_text.startswith(_COMPOSITE_FIELD_PREFIX):
                _add_composite_step(
                    add, tname, issue_text, fail_detail_map.get(issue_text, {}), resolved_set,
                    entry_meta, file_rec,
                )
            else:
                _add_singleton_fail_step(add, tname, issue_text, fail_detail_map.get(issue_text, {}), resolved_set)

        warn_batch_by_issue = {}
        for wb in file_rec.get("warn_batches") or []:
            for wi in wb.get("issues", []):
                warn_batch_by_issue[wi] = wb
        rendered_warn_batches = set()

        for iss in warn_issues_found:
            issue_text = iss.get("issue", "")
            wb = warn_batch_by_issue.get(issue_text)
            if wb is not None:
                if id(wb) in rendered_warn_batches:
                    continue
                rendered_warn_batches.add(id(wb))
                _add_batch_step(add, tname, wb, level="warn")
            else:
                _add_singleton_warn_step(add, tname, issue_text, resolved_set)

        _add_duplicate_step(add, tname, dup_issue, resolved_set)


# ── Part B: Transformation Options step builders ────────────────────────────

def _add_transformation_step(add, item: dict) -> None:
    table_name = item.get("table_name", "")
    candidate = item.get("candidate", {}) or {}
    chosen = item.get("chosen_option_id", "")
    reasoning_shown = item.get("reasoning_shown") or {}
    fresh = bool(item.get("fresh", False))
    reload_reask = bool(item.get("reload_reask", False))
    decided_at = item.get("decided_at")

    kind_label = str(candidate.get("kind", "")).replace("_", " ").title()
    columns = candidate.get("columns", [])

    if reasoning_shown.get("source") == "manual_mode":
        # Spec 3, Part 1: this decision never went through present_
        # transformation_options at all — chosen_option_id and the mapping
        # came straight from a supplied ManualModeOverride instead of a live
        # human answer. Never say "no reason was recorded" here — the
        # reference citation IS the reason, and it's always real (Part 1
        # requires reasoning_shown to carry it).
        reference = reasoning_shown.get("reference", "") or "an unnamed reference"
        supplied_data = reasoning_shown.get("supplied_data") or {}
        title = f"{kind_label} for {', '.join(columns)} — {table_name}"
        lines = [
            f"For '{', '.join(columns)}' in {table_name}, I didn't ask this live — it was "
            f"pinned directly to a supplied reference: {reference}."
        ]
        if supplied_data:
            n_groups = len(set(supplied_data.values())) if all(isinstance(v, str) for v in supplied_data.values()) else None
            if n_groups is not None:
                lines.append(
                    f"That reference supplied {len(supplied_data)} value-to-category assignments "
                    f"across {n_groups} groups, applied exactly as given."
                )
        add(
            "transformation", title, " ".join(lines),
            technical_detail=json.dumps({"chosen_option_id": chosen, "reasoning_shown": reasoning_shown}, default=str),
            stats={
                "table_name": table_name, "kind": candidate.get("kind", ""), "columns": columns,
                "fresh": fresh, "reload_reask": reload_reask, "decided_at": decided_at,
                "manual_mode": True, "reference_source": reasoning_shown.get("reference", ""),
            },
        )
        return

    context = reasoning_shown.get("context", {}) or {}
    options = reasoning_shown.get("options", []) or []

    chosen_opt = next((o for o in options if o.get("id") == chosen), None)
    chosen_label = chosen_opt["label"] if chosen_opt else chosen

    title = context.get("title") or f"{kind_label} for {', '.join(columns)} — {table_name}"

    lines = []
    what_found = context.get("what_was_found") or candidate.get("description", "")
    if what_found:
        lines.append(f"What was found: {what_found}")
    if options:
        opts_fmt = "; ".join(f"{o.get('label', o.get('id'))} — {o.get('description', '')}".strip(" —") for o in options)
        lines.append(f"I was asked to choose from: {opts_fmt}.")
    lines.append(f"I chose: {chosen_label}.")
    lines.append("No additional reason beyond this choice was recorded in the decision log.")
    if reload_reask:
        lines.append(
            "This table had just been reloaded earlier in this same run, which cleared "
            "any prior decision, so this was asked fresh again rather than reused."
        )
    elif fresh:
        lines.append("This was decided for the first time in this run.")
    else:
        when = f" (decided {decided_at})" if decided_at else ""
        lines.append(
            f"This had already been decided earlier for this dataset{when}, so it was "
            "reused silently rather than asked again."
        )

    add(
        "transformation", title, " ".join(lines),
        technical_detail=json.dumps({"chosen_option_id": chosen, "options": options}, default=str),
        stats={
            "table_name": table_name, "kind": candidate.get("kind", ""), "columns": columns,
            "fresh": fresh, "reload_reask": reload_reask, "decided_at": decided_at,
        },
    )


def _add_not_relevant_note(add, not_relevant: list) -> None:
    by_table: dict = {}
    for item in not_relevant:
        by_table.setdefault(item.get("table_name", ""), []).append(item.get("candidate", {}))

    parts = []
    for tname, cands in by_table.items():
        names = ", ".join(
            f"{str(c.get('kind', '')).replace('_', ' ')} for {', '.join(c.get('columns', []))}"
            for c in cands
        )
        parts.append(f"{tname}: {names}")

    explanation = (
        "Other optional transformations exist for this dataset (" + "; ".join(parts) + ") "
        "but weren't relevant to this particular question, so they were never surfaced or "
        "asked about here."
    )
    add(
        "transformation", "Other available transformations (not asked about here)", explanation,
        stats={"low_emphasis": True, "items": not_relevant},
    )


# ── Part C: Analysis step builders ──────────────────────────────────────────

def _add_question_shaping_step(add, sql: str) -> None:
    from utils.sql_transform_extraction import extract_question_transformations, has_any_transformation

    if not sql:
        add(
            "analysis", "Shape the data for this question",
            "No SQL was generated for this entry, so there is no question-specific "
            "shaping to describe.",
        )
        return

    t = extract_question_transformations(sql)
    if not has_any_transformation(t):
        add(
            "analysis", "Shape the data for this question",
            "This query used the source data directly — no extra grouping, computed "
            "metric, minimum sample size, ranking, or scope filter was needed beyond the "
            "general cleaning already covered above.",
            technical_detail=sql,
        )
        return

    lines = []
    if t["cte_steps"]:
        lines.append(f"I built the answer in {len(t['cte_steps'])} step(s): {' -> '.join(t['cte_steps'])}.")
    if t["scope_filters"]:
        lines.append("I scoped the data to this question specifically: " + "; ".join(t["scope_filters"]) + ".")
    if t["computed_columns"]:
        metrics = "; ".join(f"{c['alias']} = {c['expression']}" for c in t["computed_columns"])
        lines.append(f"I computed these metrics: {metrics}.")
    for group in t["grouping_columns"]:
        lines.append(f"I grouped the data by {', '.join(group)}, so each row summarizes a group, not one raw record.")
    if t["having_threshold"] is not None:
        lines.append(
            f"I excluded any group with fewer than {t['having_threshold']} underlying rows, "
            "so the ranking isn't based on too little data."
        )
    for stage in t["ranking_stages"]:
        if stage["limit"] is not None:
            lines.append(f"I ranked by {stage['order_by']} and kept the top {stage['limit']}.")
        else:
            lines.append(f"I ordered the results by {stage['order_by']}.")

    add("analysis", "Shape the data for this question", " ".join(lines), technical_detail=sql, stats=t)


def _add_final_result_step(add, entry: dict) -> None:
    final_answer = (entry.get("final_answer") or "").strip()
    rows, truncated = _parse_result_rows(entry.get("sql_query_execution_result", ""))
    stats = {
        "columns": list(rows[0].keys()) if rows else [],
        "rows": [list(r.values()) for r in rows],
        "truncated": truncated,
    }
    if final_answer:
        explanation = final_answer
    elif rows:
        explanation = "The query returned real result rows, shown in the table above."
    else:
        explanation = "No result rows are available for this entry."
    add("analysis", "The final result", explanation, stats=stats)


def _add_chart_step(add, entry: dict) -> None:
    chart_type = entry.get("chart_type", "")
    source = entry.get("chart_type_source", "")
    reasoning = entry.get("chart_type_reasoning", "")
    if source == "reasoned" and reasoning:
        explanation = f"I chose to render this as a {chart_type} because {reasoning}"
    else:
        explanation = f"You asked for a {chart_type}, so that's what I rendered."
    override_note = entry.get("chart_type_override_note", "")
    if override_note:
        explanation += f" {override_note}"
    resolution_note = entry.get("chart_column_resolution_note", "")
    if resolution_note:
        explanation += f" {resolution_note}"
    add(
        "analysis", f"Visualize the result as a {chart_type}", explanation,
        stats={
            "chart_type": chart_type, "chart_type_source": source,
            "output_file_path": entry.get("output_file_path", ""),
            "chart_image_path": entry.get("chart_image_path", ""),
        },
    )


# ── Public entry point ───────────────────────────────────────────────────────

def build_narrative_walkthrough(entry: dict) -> list:
    """Build ONE complete, ordered narrative from real data only — the single
    source of truth both report.py and presentation.py render from. Never
    invents a step that didn't happen; never omits a step that did.
    """
    steps: list = []
    counter = {"n": 0}

    def add(part: str, title: str, explanation: str, technical_detail: str = "", stats: "dict | None" = None) -> None:
        counter["n"] += 1
        steps.append(NarrativeStep(counter["n"], part, title, explanation, technical_detail, stats or {}))

    sql = entry.get("generated_sql_query", "")

    try:
        known_tables = _all_table_names()
        touched_tables = _detect_tables_in_sql(sql, known_tables) if sql else []
    except Exception:
        touched_tables = []

    try:
        table_meta = _table_metadata(touched_tables)
    except Exception:
        table_meta = []

    try:
        cleaning_map = _cleaning_entries_for_tables(touched_tables) if touched_tables else {}
    except Exception:
        cleaning_map = {t: None for t in touched_tables}

    # ── Part A: Data Cleaning ───────────────────────────────────────────────
    if table_meta:
        _build_cleaning_steps(add, table_meta, cleaning_map)
    else:
        add(
            "cleaning", "No known tables detected",
            "No known tables were detected in the generated SQL, so there is no "
            "cleaning history to walk through.",
        )

    # ── Part B: Transformation Options ──────────────────────────────────────
    for item in entry.get("transformation_narrative_log", []) or []:
        _add_transformation_step(add, item)
    not_relevant = entry.get("transformation_candidates_not_relevant", []) or []
    if not_relevant:
        _add_not_relevant_note(add, not_relevant)

    # ── Part C: Analysis ─────────────────────────────────────────────────────
    _add_question_shaping_step(add, sql)
    _add_final_result_step(add, entry)
    if entry.get("chart_type"):
        _add_chart_step(add, entry)

    return steps


# ── LLM narration pass ───────────────────────────────────────────────────────

NARRATIVE_WALKTHROUGH_PROMPT = """Rewrite each of the following data-cleaning and
data-transformation steps as first-person prose, as if you are a student explaining
your own process to a professor, in plain language someone unfamiliar with this
dataset could follow.

Use ONLY the facts given for each step — do not invent numbers, column meanings, or
reasons not stated.

For any step in the "transformation" part specifically: if a chosen reason is not
given in the facts, state only what was chosen — never invent or guess at why it was
chosen. A transformation decision with no stated reason must be narrated as a plain
factual choice, not padded with an invented justification.

Return exactly one rewritten explanation per step, in the same order, same count.

Steps:
{steps}
"""


def _step_facts_block(step: NarrativeStep) -> str:
    block = f"[{step.step_number}] ({step.part}) {step.title}\nFacts: {step.explanation}"
    if step.technical_detail:
        block += f"\nTechnical detail: {step.technical_detail}"
    if step.stats:
        try:
            block += f"\nStats: {json.dumps(step.stats, default=str)}"
        except Exception:
            pass
    return block


# ── Extra Part C content kept from earlier work, folded into the unified
# sequence per Spec 2's own "Explicitly out of scope" instruction: "No change
# to chart generation, chart-type rubric, or the glossary/second-chart
# presentation additions already working well — keep those, fold their
# existing output into Part C of the unified sequence." These two each
# already produce their own natural-language/rendered content via their own
# dedicated calls, so assemble_full_walkthrough appends them AFTER
# narrate_steps runs on the core list — they are not narrated a second time.

_COUNT_COL_RE = re.compile(r"^(?:n|count|num\w*|sample_size|total_count|\w+_count)$", re.IGNORECASE)
_MAX_GLOSSARY_TERMS = 12


def _to_float(val):
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def resolve_category_values(entry: dict) -> "tuple | None":
    """Deterministically pick the real category column out of the executed
    query's actual result (first non-numeric column, by real column order)
    and return its distinct values in first-seen order. None when the result
    has no non-numeric column at all.
    """
    rows, _truncated = _parse_result_rows(entry.get("sql_query_execution_result", ""))
    if not rows:
        return None
    cols = list(rows[0].keys())
    numeric_cols = {c for c in cols if any(_to_float(r.get(c)) is not None for r in rows)}
    non_numeric_cols = [c for c in cols if c not in numeric_cols]
    if not non_numeric_cols:
        return None
    cat_col = non_numeric_cols[0]
    seen: set = set()
    values: list = []
    for r in rows:
        v = r.get(cat_col)
        if v is None:
            continue
        v = str(v)
        if v not in seen:
            seen.add(v)
            values.append(v)
    if not values:
        return None
    return cat_col, values[:_MAX_GLOSSARY_TERMS]


def generate_glossary(category_label: str, values: list, llm) -> dict:
    """One LLM call explaining what each real category value generally MEANS
    in plain English — general background knowledge, not a dataset-derived
    claim. Returns {} on any failure or malformed response."""
    if not values:
        return {}
    terms_block = "\n".join(f"- {v}" for v in values)
    prompt = (
        f"Someone is looking at a chart grouped by \"{category_label}\" and the category "
        f"labels below aren't self-explanatory. For each one, explain in one short, plain, "
        f"conversational sentence what that label generally refers to in everyday terms — "
        f"this is general background knowledge, not something you're deriving from any "
        f"dataset, so don't state or imply any number, statistic, or dataset-specific fact.\n\n"
        f"Labels:\n{terms_block}\n\n"
        f"Respond with exactly one line per label, in this exact format, same order, "
        f"no numbering, no extra commentary:\n"
        f"<label> :: <one-sentence plain-English explanation>"
    )
    try:
        text = llm.invoke([("human", prompt)]).content
        if isinstance(text, list):
            text = "".join(
                b.get("text", "") if isinstance(b, dict) else str(b)
                for b in text if not (isinstance(b, dict) and b.get("type") == "thinking")
            )
        glossary = {}
        for line in text.splitlines():
            if "::" not in line:
                continue
            term, _, explanation = line.partition("::")
            term = term.strip().lstrip("-").strip()
            explanation = explanation.strip()
            if term and explanation:
                glossary[term] = explanation
        return glossary
    except Exception:
        return {}


def resolve_scatter_columns(entry: dict) -> "dict | None":
    """Deterministically resolve category/x/y/count columns for a second,
    volume-aware scatter view of the SAME already-executed result. None when
    the real result doesn't have the right shape."""
    rows, truncated = _parse_result_rows(entry.get("sql_query_execution_result", ""))
    if not rows or truncated:
        return None
    cols = list(rows[0].keys())
    numeric_cols = [c for c in cols if any(_to_float(r.get(c)) is not None for r in rows)]
    non_numeric_cols = [c for c in cols if c not in numeric_cols]
    if not non_numeric_cols:
        return None
    category_col = non_numeric_cols[0]
    count_col = next((c for c in numeric_cols if _COUNT_COL_RE.match(c)), None)
    if count_col is None:
        return None
    measure_cols = [c for c in numeric_cols if c != count_col]
    if len(measure_cols) < 2:
        return None
    return {
        "rows": rows, "category_col": category_col,
        "x_col": measure_cols[0], "y_col": measure_cols[1], "count_col": count_col,
    }


def render_scatter_chart_png_b64(resolved: dict) -> "str | None":
    """Render the volume-aware scatter chart in-memory; return base64 PNG
    bytes, or None if rendering fails for any reason."""
    try:
        import base64
        import io as _io

        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.figure import Figure

        rows = resolved["rows"]
        cat_col, x_col = resolved["category_col"], resolved["x_col"]
        y_col, count_col = resolved["y_col"], resolved["count_col"]

        xs, ys, sizes, labels = [], [], [], []
        for r in rows:
            x, y = _to_float(r.get(x_col)), _to_float(r.get(y_col))
            if x is None or y is None:
                continue
            n = _to_float(r.get(count_col)) or 0
            xs.append(x)
            ys.append(y)
            sizes.append(n * 6 + 40)
            labels.append(str(r.get(cat_col)))
        if not xs:
            return None

        fig = Figure(figsize=(9.5, 6.2))
        FigureCanvasAgg(fig)
        ax = fig.add_subplot(111)
        ax.scatter(xs, ys, s=sizes, alpha=0.75, edgecolors="#16213e", linewidths=0.7, zorder=3)
        for x, y, label in zip(xs, ys, labels):
            ax.annotate(label, (x, y), textcoords="offset points", xytext=(6, 4), fontsize=8)
        ax.set_xlabel(x_col.replace("_", " ").title())
        ax.set_ylabel(y_col.replace("_", " ").title())
        ax.set_title(
            f"{y_col.replace('_', ' ').title()} vs. {x_col.replace('_', ' ').title()}\n"
            f"(point size = {count_col.replace('_', ' ').title()})",
            fontsize=11,
        )
        ax.grid(True, alpha=0.25, zorder=0)
        fig.tight_layout()

        buf = _io.BytesIO()
        fig.savefig(buf, format="png", dpi=150)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _build_glossary_step(entry: dict, llm, step_number: int) -> "NarrativeStep | None":
    resolved = resolve_category_values(entry)
    if not resolved:
        return None
    cat_col, values = resolved
    glossary = generate_glossary(cat_col, values, llm)
    if not glossary:
        return None
    lines = "; ".join(f"{term}: {expl}" for term, expl in glossary.items())
    explanation = (
        f"To make the '{cat_col}' categories easier to follow, here's what each one generally "
        f"means in everyday terms (general background knowledge, not derived from this "
        f"dataset): {lines}"
    )
    return NarrativeStep(
        step_number, "analysis", "What do these categories mean?", explanation,
        stats={"glossary": glossary, "category_col": cat_col},
    )


def _build_scatter_step(entry: dict, step_number: int) -> "NarrativeStep | None":
    resolved = resolve_scatter_columns(entry)
    if not resolved:
        return None
    chart_b64 = render_scatter_chart_png_b64(resolved)
    if not chart_b64:
        return None
    x_col, y_col, count_col = resolved["x_col"], resolved["y_col"], resolved["count_col"]
    explanation = (
        f"Here's the same result plotted a different way — {y_col} against {x_col}, with "
        f"each point's size showing {count_col}, so a category perched on an extreme value "
        f"backed by very few real records stands out from one backed by a lot of them."
    )
    return NarrativeStep(
        step_number, "analysis", "Scatter, sized by volume", explanation,
        stats={
            "chart_b64": chart_b64, "x_col": x_col, "y_col": y_col, "count_col": count_col,
            "category_col": resolved["category_col"],
        },
    )


def assemble_full_walkthrough(entry: dict, llm) -> list:
    """The single call both generate_report.py and generate_presentation.py
    use: build_narrative_walkthrough + narrate_steps, THEN fold in the
    glossary/second-chart additions Spec 2 explicitly keeps (see the module
    comment above) immediately around the chart step, renumbered into one
    contiguous sequence — so both documents always render the exact same
    steps in the exact same order.
    """
    steps = build_narrative_walkthrough(entry)
    steps = narrate_steps(steps, llm)

    if not entry.get("chart_type"):
        return steps

    chart_idx = next((i for i, s in enumerate(steps) if s.title.startswith("Visualize the result")), None)
    if chart_idx is None:
        return steps

    extra_before = []
    try:
        glossary_step = _build_glossary_step(entry, llm, 0)
    except Exception:
        glossary_step = None
    if glossary_step is not None:
        extra_before.append(glossary_step)

    extra_after = []
    try:
        scatter_step = _build_scatter_step(entry, 0)
    except Exception:
        scatter_step = None
    if scatter_step is not None:
        extra_after.append(scatter_step)

    new_order = steps[:chart_idx] + extra_before + [steps[chart_idx]] + extra_after + steps[chart_idx + 1:]
    return [
        NarrativeStep(i + 1, s.part, s.title, s.explanation, s.technical_detail, s.stats)
        for i, s in enumerate(new_order)
    ]


def narrate_steps(steps: list, llm) -> list:
    """Rewrite every step's explanation via ONE combined, structured-output LLM
    call — never one call per step (cheaper, more consistent tone). Validates
    the returned list's length against the input before using it; on any
    mismatch or failure, falls back to the deterministic (unnarrated but
    factual) explanation text for every step rather than silently dropping
    one.
    """
    if not steps:
        return steps

    from pydantic import BaseModel

    class _NarratedSteps(BaseModel):
        explanations: list[str]

    steps_block = "\n\n".join(_step_facts_block(s) for s in steps)
    prompt = NARRATIVE_WALKTHROUGH_PROMPT.format(steps=steps_block)

    try:
        structured = llm.with_structured_output(_NarratedSteps).invoke([("human", prompt)])
        narrated = structured.explanations
    except Exception:
        return steps

    if not isinstance(narrated, list) or len(narrated) != len(steps):
        return steps

    result = []
    for i, s in enumerate(steps):
        text = (narrated[i] or "").strip() if i < len(narrated) else ""
        result.append(
            NarrativeStep(
                s.step_number, s.part, s.title, text or s.explanation,
                s.technical_detail, s.stats,
            )
        )
    return result
