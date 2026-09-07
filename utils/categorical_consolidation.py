"""Spec 3, Part 2 — categorical_consolidation: a new TransformationCandidate
kind for a high-cardinality categorical column that could be grouped into a
smaller set of human-meaningful categories (e.g. 57 raw Industry values into
a handful of sector groups).

Structurally mirrors utils/feature_derivation.py's discipline (own detector,
own opt-in apply step, never called automatically by clean_dataset()), with
one real difference: APPLYING a resolved mapping is a pure, deterministic
value substitution (pandas .map()) once the mapping itself is known — no
LLM/code-gen is needed for the apply step, only (optionally) for PRODUCING
the mapping in the live, non-manual-mode path (_generate_categorical_
consolidation_mapping). Manual mode (Spec 3, Parts 1/5) supplies the mapping
directly instead, skipping that LLM call entirely — see
agents/sql_analyst.py:_apply_chosen_transformation's categorical_consolidation
branch.
"""

import pandas as pd

CATEGORICAL_CONSOLIDATION_MIN_DISTINCT = 15
CATEGORICAL_CONSOLIDATION_MIN_RATIO = 0.03
CATEGORICAL_CONSOLIDATION_MAX_RATIO = 0.9


def detect_categorical_consolidation_candidates(df: pd.DataFrame) -> list:
    """Deterministic, no-LLM detector (same discipline as check_rubric /
    _detect_range_columns): flags a categorical (non-numeric) column as a
    consolidation candidate when its cardinality is "high relative to row
    count" — concretely, at least CATEGORICAL_CONSOLIDATION_MIN_DISTINCT
    distinct real values, and a distinct/row ratio between
    CATEGORICAL_CONSOLIDATION_MIN_RATIO (excludes small, already-manageable
    categorical columns) and CATEGORICAL_CONSOLIDATION_MAX_RATIO (excludes
    near-unique/free-text/id-like columns, which aren't a consolidation
    candidate at all — they're a different problem).

    Returns [{"column", "distinct_count", "row_count", "sample_values"}, ...].
    """
    from utils.data_cleaning import _column_value_type_category

    row_count = len(df)
    if row_count == 0:
        return []

    out = []
    for col in df.columns:
        series = df[col].dropna()
        if series.empty:
            continue
        if _column_value_type_category(series) == "numeric":
            continue
        distinct_count = series.nunique()
        ratio = distinct_count / row_count
        if distinct_count < CATEGORICAL_CONSOLIDATION_MIN_DISTINCT:
            continue
        if not (CATEGORICAL_CONSOLIDATION_MIN_RATIO <= ratio <= CATEGORICAL_CONSOLIDATION_MAX_RATIO):
            continue
        sample_values = sorted(series.unique().tolist())[:10]
        out.append({
            "column": col,
            "distinct_count": int(distinct_count),
            "row_count": row_count,
            "sample_values": sample_values,
        })
    return out


CATEGORICAL_CONSOLIDATION_SYSTEM_PROMPT = """You are grouping the real distinct \
values of one categorical column into a small number of human-meaningful \
categories, for a data analysis pipeline.

You will be given EVERY real distinct value in the column. Assign EVERY \
SINGLE ONE of them to exactly one group — never skip a value, never invent a \
value that wasn't given to you. Choose a reasonable number of groups (usually \
5-10) with clear, short, human-readable names. Group semantically similar \
values together (e.g. similar industries, similar job seniority levels) using \
your general world knowledge — this is a judgment call, not a fact lookup, so \
there is no single "correct" answer, just a reasonable one.

Return one assignment per real distinct value given to you, in the same \
order, covering all of them."""


def _generate_categorical_consolidation_mapping(distinct_values: list, llm) -> "dict | None":
    """ONE structured-output LLM call (live, non-manual-mode path only) that
    groups every real distinct value into a category. Returns a
    dict[raw_value, group] covering EXACTLY the given distinct_values — or
    None if the LLM call fails or its response doesn't mechanically cover
    every real value (never silently applies a partial/wrong mapping; the
    caller must treat None as "could not produce a mapping this way").
    """
    from models.schema import CategoricalConsolidationProposal

    if not distinct_values:
        return None

    values_block = "\n".join(f"- {v}" for v in distinct_values)
    prompt = (
        f"Here are all {len(distinct_values)} real distinct values in this column:\n\n"
        f"{values_block}\n\n"
        f"Assign every one of them to a group, per the instructions."
    )
    try:
        proposal = llm.with_structured_output(CategoricalConsolidationProposal).invoke(
            [("system", CATEGORICAL_CONSOLIDATION_SYSTEM_PROMPT), ("human", prompt)]
        )
    except Exception:
        return None

    mapping = {a.raw_value: a.group for a in proposal.assignments}
    if set(mapping.keys()) != set(distinct_values):
        return None
    return mapping


def consolidate_categorical_column(file_path, candidate, mapping: dict, new_column_name: str = "") -> dict:
    """THE APPLY STEP — deterministic, no LLM/code-gen involved (the mapping
    is already known, whether from live AI clustering or a manual-mode
    override; applying it is a pure pandas .map()). Adds ONE new column
    (default f"{column}_category") holding the mapped group for each row;
    the original column is preserved untouched. A raw value with no entry in
    `mapping` becomes null in the new column — never fabricated — and is
    reported back via unmapped_values/unmapped_count so a caller can decide
    whether that's acceptable (e.g. a manual-mode mapping that's missing a
    genuinely new raw value the reference never covered).

    Returns {"column", "new_column", "status", "unmapped_values", "unmapped_count"}.
    `status` is "resolved" (mapping applied, file written) or "error" (I/O
    failure) — never a retry loop, since there's no generated code to retry.
    """
    from utils.data_cleaning import _read_csv_robust

    column = candidate.columns[0]
    new_column = new_column_name or f"{column}_category"

    try:
        df = _read_csv_robust(file_path)
        if column not in df.columns:
            return {
                "column": column, "new_column": new_column, "status": "error",
                "unmapped_values": [], "unmapped_count": 0,
                "error": f"Column '{column}' not found in {file_path}.",
            }
        mapped = df[column].map(mapping)
        real_values = set(df[column].dropna().unique().tolist())
        unmapped_values = sorted(v for v in real_values if v not in mapping)
        df[new_column] = mapped
        df.to_csv(file_path, index=False)
        return {
            "column": column, "new_column": new_column, "status": "resolved",
            "unmapped_values": unmapped_values, "unmapped_count": len(unmapped_values),
        }
    except Exception as e:
        return {
            "column": column, "new_column": new_column, "status": "error",
            "unmapped_values": [], "unmapped_count": 0, "error": str(e),
        }
