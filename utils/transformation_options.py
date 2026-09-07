"""
Spec 1, Parts 0 / 0.5 / 1 — Transformation Options: the second, human-in-the-
loop pipeline phase for judgment-call enrichment/style decisions, kept
structurally separate from Phase 1 (clean_dataset()'s objectively-wrong-data
fixes, which stay fully automatic — see utils/data_cleaning.py). "Why two
phases, not one" (project's own reasoning, echoed here): bolting interactive
enrichment choices onto clean_dataset() would blur two fundamentally
different operations and break the reproducibility guarantee that made
missing-value strategy a fixed rule instead of a per-run question.

Part 0 — unified candidate detection: every optional transformation (range
decomposition today via _detect_range_decomposition_candidates; feature-
derivation and label-simplification candidates are a later spec, see the
module docstring in feature_derivation.py once that exists) is represented as
one shared TransformationCandidate shape, detected once per table right
after Phase 1 cleaning finishes (detect_transformation_candidates), and
persisted to _transformation_candidates (utils/load_data.py) — never
re-detected live at question time.

Part 0.5 — question-driven surfacing: surface_relevant_transformations
filters the STORED candidate list down to only what a specific question/
chart actually touches, matched generically against each candidate's
relevance_tags/columns — no per-candidate-kind special-casing, so a future
candidate kind needs no change here.

Part 1 — decision cache + presentation: once a human decides a candidate
(present_transformation_options), that decision is durably cached in
_transformation_decisions (utils/load_data.py), keyed by
(table_name, candidate_id), and reused silently for every future question
touching the same candidate — never re-asked, unless a genuine reload
invalidates it (utils/load_data.py:invalidate_cached_decisions_for_table).

Live wiring note: this module currently has exactly one real candidate kind
(range_decomposition, built on the already-existing
utils.data_cleaning._detect_range_columns). Parts 5/7/8 of this spec add
feature-derivation and label-simplification detectors as later work; they
plug into detect_transformation_candidates the same way — see its docstring.
Hooking surface_relevant_transformations/present_transformation_options into
the live SQL-analyst question flow (agents/sql_analyst.py, alongside
resolve_chart_columns/determine_chart_type) is also later work, deferred
until there's more than one candidate kind worth surfacing live.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field


@dataclass
class TransformationCandidate:
    """One optional, judgment-call transformation detected for a table.

    candidate_id: deterministic, stable across re-scans of the same
    underlying data — see compute_candidate_id.
    kind: "range_decomposition" | "feature_derivation" | "label_simplification"
    | a future kind.
    columns: the real column(s) this candidate concerns.
    description: real facts backing this candidate (match %, sample values,
    pattern found) — never invented. Shown to a human verbatim by
    present_transformation_options.
    relevance_tags: generic tags (e.g. ["salary", "compensation"]) matched
    against a question's intent/touched columns by
    surface_relevant_transformations — not hardcoded per-candidate-kind
    logic downstream.
    """

    candidate_id: str
    kind: str
    columns: list = field(default_factory=list)
    description: str = ""
    relevance_tags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate_id,
            "kind": self.kind,
            "columns": list(self.columns),
            "description": self.description,
            "relevance_tags": list(self.relevance_tags),
        }

    @staticmethod
    def from_dict(d: dict) -> "TransformationCandidate":
        return TransformationCandidate(
            candidate_id=d["candidate_id"],
            kind=d["kind"],
            columns=list(d.get("columns", [])),
            description=d.get("description", ""),
            relevance_tags=list(d.get("relevance_tags", [])),
        )


def compute_candidate_id(table_name: str, kind: str, columns) -> str:
    """Deterministic, stable across re-scans AND across process
    restarts/sessions: sha256 over (table_name, kind, sorted(columns)).

    The spec describes this as `hash((table_name, kind, tuple(sorted(columns))))`,
    but Python's built-in hash() is salted per-process for str objects by
    default (PYTHONHASHSEED) — literally using it would make the id UNSTABLE
    across the very "across sessions" boundary the decision cache (Part 1)
    depends on. sha256 gives the same stability property the spec actually
    asks for, not just its pseudocode's surface form.
    """
    payload = json.dumps(
        {"table": table_name, "kind": kind, "columns": sorted(columns)},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Part 0: unified detection
# ---------------------------------------------------------------------------

_RANGE_RELEVANCE_TAG_HINTS = {
    "salary": ("salary", "compensation"),
    "wage": ("salary", "compensation"),
    "pay": ("salary", "compensation"),
    "compensation": ("salary", "compensation"),
    "revenue": ("revenue", "income"),
    "income": ("revenue", "income"),
    "price": ("price", "compensation"),
}


def _relevance_tags_for_range_column(column: str) -> list:
    """Generic relevance tags for a range-decomposition candidate, derived
    from the column's own name — e.g. "Salary Estimate" -> salary/
    compensation tags, "Revenue" -> revenue/income tags. Falls back to the
    lowercased column name itself when no hint matches, so an unanticipated
    column name still gets SOME real tag rather than none."""
    col_lower = column.lower()
    tags = set()
    for hint, hint_tags in _RANGE_RELEVANCE_TAG_HINTS.items():
        if hint in col_lower:
            tags.update(hint_tags)
    if not tags:
        tags.add(col_lower)
    return sorted(tags)


def _detect_range_decomposition_candidates(df, table_name: str) -> list:
    """Wraps the already-existing, already-tested
    utils.data_cleaning._detect_range_columns detector (Spec 3) into the
    shared TransformationCandidate shape — no detection logic duplicated."""
    from utils.data_cleaning import _detect_range_columns

    out = []
    for entry in _detect_range_columns(df):
        column = entry["column"]
        description = (
            f"Column '{column}' has {entry['match_fraction']:.0%} of its real "
            f"values matching a numeric range pattern (sample: "
            f"{entry['sample_pattern']!r}) — could be decomposed into separate "
            "min/max/avg numeric columns."
        )
        out.append(
            TransformationCandidate(
                candidate_id=compute_candidate_id(table_name, "range_decomposition", [column]),
                kind="range_decomposition",
                columns=[column],
                description=description,
                relevance_tags=_relevance_tags_for_range_column(column),
            )
        )
    return out


# relevance_tags per feature-derivation kind, exactly as specified (Part 7) —
# generic across the shared surfacing logic, no per-kind special-casing
# downstream in surface_relevant_transformations.
_FEATURE_DERIVATION_RELEVANCE_TAGS = {
    "job_title_categorization": ["job title", "role", "position"],
    "company_age": ["company age", "founding", "tenure"],
    "same_state_flag": ["location", "headquarters", "geography"],
    "skill_keywords": ["skill", "technology", "tools"],
    "seniority_flag": ["seniority", "experience level"],
}


def _detect_feature_derivation_wrapped_candidates(df, table_name: str) -> list:
    """Wraps utils.feature_derivation.detect_feature_derivation_candidates
    (Part 7) into the shared TransformationCandidate shape — no detection
    logic duplicated. Each of the 5 kinds gets its own candidate_id, its own
    independent relevance_tags, and is offered independently (Part 7's own
    requirement — never one bundled 'derive stuff' candidate)."""
    from utils.feature_derivation import detect_feature_derivation_candidates

    out = []
    for entry in detect_feature_derivation_candidates(df):
        kind = entry["kind"]
        out.append(
            TransformationCandidate(
                candidate_id=compute_candidate_id(table_name, kind, entry["columns"]),
                kind=kind,
                columns=entry["columns"],
                description=entry["description"],
                relevance_tags=_FEATURE_DERIVATION_RELEVANCE_TAGS[kind],
            )
        )
    return out


def _relevance_tags_for_label_column(column: str) -> list:
    """Generic relevance tags for a label-simplification candidate, derived
    from the column's own name (Part 8) — e.g. "Size" -> ["size", "company
    size"]. Falls back to the lowercased column name alone if no hint
    matches."""
    col_lower = column.lower()
    tags = {col_lower}
    if "size" in col_lower:
        tags.add("company size")
    return sorted(tags)


def _detect_label_simplification_wrapped_candidates(df, table_name: str) -> list:
    """Wraps the deterministic utils.data_cleaning._detect_label_simplification_columns
    detector (Part 8) into the shared TransformationCandidate shape — no
    detection logic duplicated."""
    from utils.data_cleaning import _detect_label_simplification_columns

    out = []
    for entry in _detect_label_simplification_columns(df):
        column = entry["column"]
        description = (
            f"Column '{column}' has {entry['match_fraction']:.0%} of its real "
            f"values matching a verbose 'N [to M] <unit>' label shape (sample: "
            f"{entry['sample_value']!r}) — could be shortened to a more compact "
            "equivalent style (e.g. \"51 to 200 employees\" -> \"51-200\")."
        )
        out.append(
            TransformationCandidate(
                candidate_id=compute_candidate_id(table_name, "label_simplification", [column]),
                kind="label_simplification",
                columns=[column],
                description=description,
                relevance_tags=_relevance_tags_for_label_column(column),
            )
        )
    return out


def detect_transformation_candidates(df, table_name: str) -> list:
    """Runs ONCE per table, immediately after clean_dataset() finishes on a
    genuinely fresh load — never per-question, never per-decision-check.
    Consolidates every candidate-kind detector into one shared output list
    (list[TransformationCandidate]), so downstream filtering
    (surface_relevant_transformations) never needs to special-case a
    candidate kind's internal shape.

    Calls _detect_range_decomposition_candidates (Part 6),
    _detect_feature_derivation_wrapped_candidates (Part 7, 5 independent
    kinds), and _detect_label_simplification_wrapped_candidates (Part 8) —
    every candidate kind this spec defines.
    """
    candidates = []
    candidates.extend(_detect_range_decomposition_candidates(df, table_name))
    candidates.extend(_detect_feature_derivation_wrapped_candidates(df, table_name))
    candidates.extend(_detect_label_simplification_wrapped_candidates(df, table_name))
    return candidates


# ---------------------------------------------------------------------------
# Part 0.5: question-driven surfacing
# ---------------------------------------------------------------------------


def _normalize_tag(text: str) -> str:
    return text.strip().lower()


def _normalize_column(name: str) -> str:
    """Normalize a column name for relevance matching, tolerant of raw-CSV-
    name vs. sanitized-DB-column-name spelling differences (e.g. "Salary
    Estimate" vs. "salary_estimate") — lowercase, non-alphanumeric characters
    collapsed to single spaces."""
    return re.sub(r"[^a-z0-9]+", " ", name.strip().lower()).strip()


def surface_relevant_transformations(
    curated_question: str,
    chart_category_column: str,
    chart_value_column: str,
    touched_columns: list,
    candidates: list,
) -> list:
    """Filter STORED candidates (read from _transformation_candidates, never
    re-detected here) down to only those relevant to THIS question/chart.

    Relevance is generic across every candidate kind — never a per-kind
    special case:
    1. Column match: any of the candidate's own `columns` case-insensitively
       matches (exact, or one contains the other, to tolerate raw-CSV-name
       vs. sanitized-DB-column-name spelling differences — e.g. "Salary
       Estimate" vs. "salary_estimate") any of touched_columns,
       chart_category_column, or chart_value_column.
    2. Tag match: any of the candidate's relevance_tags appears as a
       substring of the curated_question (case-insensitive) — catches a
       question that names the topic ("salary", "seniority") without the
       exact column surfacing in touched_columns.

    A candidate needs only ONE of these to match to be surfaced. Order is
    preserved from the input `candidates` list.
    """
    question_lower = (curated_question or "").lower()
    column_pool = [c for c in (list(touched_columns or []) + [chart_category_column, chart_value_column]) if c]
    normalized_pool = [_normalize_column(c) for c in column_pool]

    def _column_matches(candidate_col: str) -> bool:
        cand_norm = _normalize_column(candidate_col)
        if not cand_norm:
            return False
        for pool_col in normalized_pool:
            if cand_norm == pool_col or cand_norm in pool_col or pool_col in cand_norm:
                return True
        return False

    relevant = []
    for candidate in candidates:
        if any(_column_matches(col) for col in candidate.columns):
            relevant.append(candidate)
            continue
        if any(_normalize_tag(tag) in question_lower for tag in candidate.relevance_tags if tag):
            relevant.append(candidate)
    return relevant


# ---------------------------------------------------------------------------
# Part 1: decision cache + presentation
# ---------------------------------------------------------------------------

_SKIP_OPTION = {"id": "skip", "label": "Skip / do nothing", "description": "Leave the data as-is."}


def present_transformation_options(
    table_name: str,
    candidate_id: str,
    context: dict,
    options: list,
    conn=None,
) -> dict:
    """Present a judgment-call transformation decision to a human, with real
    context and a small set of reasonable options, and block on a real
    answer — mirrors _request_approval's discipline (real input(), not a
    config flag, not bypassable) but for CHOOSING an approach, not approving
    generated code.

    context: {"title": str, "what_was_found": str, "why_optional": str}
    options: list of {"id": str, "label": str, "description": str}. An
    implicit "skip / do nothing" option is always appended.

    Returns {"chosen_option_id": str, "reasoning_shown": {"context": ...,
    "options": ...}} — the full context and options actually shown are
    preserved in the return value (and in the persisted decision row) so the
    decision is fully reconstructable later, not just "user said B".

    table_name/candidate_id identify which stored candidate this decision is
    for (beyond the spec's illustrative context/options sketch) — required
    to persist into _transformation_decisions, keyed by
    (table_name, candidate_id), with the same durability as every other
    logged decision in this pipeline (Part 1).

    conn: an already-open ADMIN connection (this decision is a durable write,
    and app_reader is DB-enforced read-only — see AGENTS.md). When None
    (the normal case), opens and closes one internally via
    utils.load_data.get_admin_connection(); when given (tests, or a future
    orchestrator that already holds one open), the caller owns its lifecycle.
    """
    all_options = list(options) + [_SKIP_OPTION]

    print("=" * 70)
    print(f"TRANSFORMATION OPTION: {context.get('title', '')}")
    print("=" * 70)
    if context.get("what_was_found"):
        print(f"What was found: {context['what_was_found']}")
    if context.get("why_optional"):
        print(f"Why this is optional: {context['why_optional']}")
    print()
    for opt in all_options:
        print(f"  [{opt['id']}] {opt['label']} — {opt.get('description', '')}")
    valid_ids = {opt["id"] for opt in all_options}

    chosen_option_id = None
    while chosen_option_id is None:
        answer = input(f"Choose an option ({'/'.join(sorted(valid_ids))}): ").strip()
        if answer in valid_ids:
            chosen_option_id = answer
        else:
            print(f"'{answer}' is not one of the listed option ids — try again.")

    reasoning_shown = {"context": dict(context), "options": all_options}
    decision = {"chosen_option_id": chosen_option_id, "reasoning_shown": reasoning_shown}

    from utils.load_data import write_transformation_decision

    owns_conn = conn is None
    if owns_conn:
        from utils.load_data import get_admin_connection

        conn = get_admin_connection()
    try:
        write_transformation_decision(conn, table_name, candidate_id, decision)
    finally:
        if owns_conn:
            conn.close()

    return decision
