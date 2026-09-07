"""Spec 3, Part 2: tests utils/categorical_consolidation.py in isolation —
detection thresholds, live AI-clustering mapping generation (fake LLM, no
network), and the deterministic apply step. No manual mode, no DB — those
are covered by test_manual_mode.py.
"""

import json
import tempfile
from pathlib import Path

import pandas as pd

from models.schema import CategoricalAssignment, CategoricalConsolidationProposal
from utils.categorical_consolidation import (
    CATEGORICAL_CONSOLIDATION_MIN_DISTINCT,
    _generate_categorical_consolidation_mapping,
    consolidate_categorical_column,
    detect_categorical_consolidation_candidates,
)

print("=" * 70)
print("TEST 1: detect_categorical_consolidation_candidates thresholds")
print("=" * 70)

# 20 rows, an "industry"-like column with 15 distinct values (ratio 0.75,
# within [0.03, 0.9]) must be flagged; a "size" column with only 3 distinct
# values must NOT be flagged (below MIN_DISTINCT); a near-unique "id_text"
# column (20/20 = 1.0 ratio) must NOT be flagged (above MAX_RATIO).
n = 20
industries = [f"industry_{i % 15}" for i in range(n)]
sizes = [["small", "medium", "large"][i % 3] for i in range(n)]
id_text = [f"row_{i}" for i in range(n)]
numeric_col = [str(i) for i in range(n)]

df1 = pd.DataFrame({"industry": industries, "size": sizes, "id_text": id_text, "numeric_col": numeric_col})
candidates1 = detect_categorical_consolidation_candidates(df1)
flagged_cols = {c["column"] for c in candidates1}

assert "industry" in flagged_cols, f"expected 'industry' flagged, got {flagged_cols}"
assert "size" not in flagged_cols, f"'size' has too few distinct values, must not be flagged: {flagged_cols}"
assert "id_text" not in flagged_cols, f"'id_text' is near-unique, must not be flagged: {flagged_cols}"
assert "numeric_col" not in flagged_cols, f"a numeric column must never be flagged: {flagged_cols}"

industry_cand = next(c for c in candidates1 if c["column"] == "industry")
assert industry_cand["distinct_count"] == 15
assert industry_cand["row_count"] == 20
print(f"PASS: thresholds correctly select 'industry' (15/20) and exclude 'size' (3/20), "
      f"'id_text' (20/20), and a numeric column.\n")

# Real dataset shape sanity check: MIN_DISTINCT itself.
assert CATEGORICAL_CONSOLIDATION_MIN_DISTINCT == 15
print(f"PASS: CATEGORICAL_CONSOLIDATION_MIN_DISTINCT = {CATEGORICAL_CONSOLIDATION_MIN_DISTINCT} "
      f"(real Industry column: 57 distinct / 672 rows; real Job Title: 172/672 — both clear it).\n")


print("=" * 70)
print("TEST 2: _generate_categorical_consolidation_mapping — happy path,")
print("mismatch rejection, LLM-failure handling")
print("=" * 70)

distinct_values = [f"industry_{i}" for i in range(15)]


class _GoodClusterLLM:
    def with_structured_output(self, schema_cls):
        return self

    def invoke(self, messages):
        return CategoricalConsolidationProposal(
            assignments=[
                CategoricalAssignment(raw_value=v, group=f"Group {i % 3}")
                for i, v in enumerate(distinct_values)
            ]
        )


mapping = _generate_categorical_consolidation_mapping(distinct_values, _GoodClusterLLM())
assert mapping is not None
assert set(mapping.keys()) == set(distinct_values)
assert len(set(mapping.values())) == 3
print(f"PASS: a real, complete proposal covering all {len(distinct_values)} values is accepted.\n")


class _IncompleteClusterLLM:
    def with_structured_output(self, schema_cls):
        return self

    def invoke(self, messages):
        # Only covers the first 10 of 15 values — must be rejected, not
        # silently applied as a partial mapping.
        return CategoricalConsolidationProposal(
            assignments=[
                CategoricalAssignment(raw_value=v, group="Group 0")
                for v in distinct_values[:10]
            ]
        )


mapping_bad = _generate_categorical_consolidation_mapping(distinct_values, _IncompleteClusterLLM())
assert mapping_bad is None, "an incomplete mapping must be rejected (None), never partially applied"
print("PASS: an incomplete LLM response is rejected as None, never partially trusted.\n")


class _RaisingLLM:
    def with_structured_output(self, schema_cls):
        return self

    def invoke(self, messages):
        raise RuntimeError("simulated LLM failure")


mapping_fail = _generate_categorical_consolidation_mapping(distinct_values, _RaisingLLM())
assert mapping_fail is None
print("PASS: an LLM exception is caught and yields None, never raises.\n")

assert _generate_categorical_consolidation_mapping([], _GoodClusterLLM()) is None
print("PASS: no distinct values -> None, no LLM call needed.\n")


print("=" * 70)
print("TEST 3: consolidate_categorical_column — deterministic apply, real file")
print("=" * 70)


class _FakeCandidate:
    def __init__(self, columns):
        self.columns = columns


with tempfile.TemporaryDirectory() as tmpdir:
    csv_path = Path(tmpdir) / "data.csv"
    df = pd.DataFrame({
        "Industry": ["Tech", "Finance", "Tech", "Healthcare", "Unknown Industry"],
        "other_col": [1, 2, 3, 4, 5],
    })
    df.to_csv(csv_path, index=False)

    real_mapping = {"Tech": "Technology & Software", "Finance": "Finance & Insurance", "Healthcare": "Healthcare"}
    candidate = _FakeCandidate(columns=["Industry"])
    result = consolidate_categorical_column(str(csv_path), candidate, real_mapping)

    assert result["status"] == "resolved", result
    assert result["new_column"] == "Industry_category"
    assert result["unmapped_count"] == 1
    assert result["unmapped_values"] == ["Unknown Industry"], (
        "the one raw value with no mapping entry must be reported, not silently dropped"
    )

    written = pd.read_csv(csv_path, dtype=str)
    assert "Industry" in written.columns, "the original column must be preserved untouched"
    assert list(written["Industry"]) == ["Tech", "Finance", "Tech", "Healthcare", "Unknown Industry"]
    assert list(written["Industry_category"]) == [
        "Technology & Software", "Finance & Insurance", "Technology & Software",
        "Healthcare", None,
    ] or written["Industry_category"].isna().iloc[4], (
        "the unmapped row must be null, never a fabricated group"
    )
    assert pd.isna(written["Industry_category"].iloc[4]), "unmapped raw value must produce a null category, not a guess"
    print(f"PASS: real file written with a real new column, original column untouched, "
          f"unmapped value correctly left null: {result}\n")

    # A missing flagged column must fail cleanly, not crash.
    bad_candidate = _FakeCandidate(columns=["DoesNotExist"])
    result_bad = consolidate_categorical_column(str(csv_path), bad_candidate, {})
    assert result_bad["status"] == "error"
    print("PASS: a missing flagged column produces a clean error result, no crash.\n")

print("=" * 70)
print("ALL CATEGORICAL-CONSOLIDATION (SPEC 3, PART 2) ASSERTIONS PASSED")
print("=" * 70)
