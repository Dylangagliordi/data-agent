"""Tests for categorical imputation method selection (Rubric item 7 refinement).

Unit tests (no LLM) cover:
  - _categorical_fill_advice: dominant distribution → mode recommendation
  - _categorical_fill_advice: even distribution → 'Unknown' recommendation
  - _categorical_fill_advice: numeric column → no advice (not categorical)
  - _issue_guidance with df: dominant categorical column → mode note appended
  - _issue_guidance with df: even categorical column → 'Unknown' note appended
  - _issue_guidance without df: backward-compatible (no categorical note)

Integration test (real LLM) covers:
  - Even-distribution categorical column actually filled with 'Unknown' in generated code

Run with:
    printf 'yes\\n' | uv run python -m tests.test_categorical_fill_advice
"""

import io
import pandas as pd

from utils.data_cleaning import (
    MISSING_VALUE_IMPUTE_CEILING,
    _categorical_fill_advice,
    _issue_guidance,
    _CATEGORICAL_DOMINANT_THRESHOLD,
    check_rubric,
    clean_dataset,
)

SEP = "=" * 70

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _dominant_df():
    """Column 'status' has a clear majority: 'active' appears 60% of the time."""
    return pd.DataFrame({
        "status": ["active"] * 6 + ["inactive"] * 2 + ["pending"] * 2,
    })


def _even_df():
    """Column 'status' is evenly split: each category is exactly 25%."""
    return pd.DataFrame({
        "status": ["active"] * 3 + ["inactive"] * 3 + ["pending"] * 3 + ["closed"] * 3,
    })


def _numeric_df():
    return pd.DataFrame({"amount": [100.0, 200.0, 300.0, 400.0]})


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

print(SEP)
print("UNIT: _categorical_fill_advice")
print(SEP)

# Dominant distribution → mode
df_dom = _dominant_df()
advice_dom = _categorical_fill_advice(df_dom, "status")
print("dominant advice:", advice_dom)
assert "active" in advice_dom, f"expected mode 'active' in advice: {advice_dom}"
assert "Unknown" not in advice_dom, f"should NOT say Unknown for dominant dist: {advice_dom}"
assert "mode" in advice_dom.lower() or "active" in advice_dom, advice_dom
print("PASS: dominant distribution → mode recommendation\n")

# Even distribution → 'Unknown'
df_even = _even_df()
advice_even = _categorical_fill_advice(df_even, "status")
print("even advice:", advice_even)
assert "Unknown" in advice_even, f"expected 'Unknown' in advice: {advice_even}"
assert "roughly even" in advice_even or "even" in advice_even.lower(), advice_even
print("PASS: even distribution → 'Unknown' recommendation\n")

# Numeric column → no advice
df_num = _numeric_df()
advice_num = _categorical_fill_advice(df_num, "amount")
print("numeric advice:", repr(advice_num))
assert advice_num == "", f"numeric column should produce no advice: {advice_num}"
print("PASS: numeric column → no advice\n")

# Missing column → no advice
advice_missing = _categorical_fill_advice(df_dom, "nonexistent_col")
assert advice_missing == "", f"missing column should produce no advice: {advice_missing}"
print("PASS: nonexistent column → no advice\n")

# ---------------------------------------------------------------------------
# Unit tests: _issue_guidance with df
# ---------------------------------------------------------------------------

print(SEP)
print("UNIT: _issue_guidance with df argument")
print(SEP)

issue_tmpl = "Missing values: column 'status' is 12.0% missing/blank (threshold 5%)."

# Dominant: guidance should contain mode recommendation
df_with_dom = pd.DataFrame({
    "status": ["active"] * 6 + ["inactive"] * 2 + ["pending"] * 2,
})
guidance_dom = _issue_guidance(issue_tmpl, df=df_with_dom)
print("guidance (dominant):", guidance_dom)
assert "imputing is reasonable" in guidance_dom
assert "active" in guidance_dom, f"expected mode 'active' in guidance: {guidance_dom}"
assert "Unknown" not in guidance_dom
print("PASS: dominant categorical column → mode note in guidance\n")

# Even: guidance should contain 'Unknown'
df_with_even = _even_df()
# Add the 'status' column (already present) — reuse _even_df
guidance_even = _issue_guidance(issue_tmpl, df=df_with_even)
print("guidance (even):", guidance_even)
assert "imputing is reasonable" in guidance_even
assert "Unknown" in guidance_even, f"expected 'Unknown' in guidance: {guidance_even}"
print("PASS: even categorical column → 'Unknown' note in guidance\n")

# Without df: backward-compatible, no fill-value note
guidance_no_df = _issue_guidance(issue_tmpl)
print("guidance (no df):", guidance_no_df)
assert "imputing is reasonable" in guidance_no_df
# Neither "Unknown" nor a mode value should appear when we have no distribution data
assert "active" not in guidance_no_df
assert "Unknown" not in guidance_no_df
print("PASS: no df → guidance still works, no fill-value note\n")

# High-pct issue: unchanged path (df doesn't matter)
high_issue = "Missing values: column 'status' is 45.0% missing/blank (threshold 5%)."
guidance_high = _issue_guidance(high_issue, df=df_with_dom)
assert "do NOT silently impute" in guidance_high
print("PASS: high-pct issue → do-not-impute path unaffected\n")

# ---------------------------------------------------------------------------
# Integration test: real LLM fills even-distribution column with 'Unknown'
# ---------------------------------------------------------------------------

print(SEP)
print("INTEGRATION: even-distribution categorical column → filled with 'Unknown' in real output")
print(SEP)

import tempfile, pathlib, os, shutil

# Build a small fixture CSV with a categorical 'status' column that is:
#   - roughly even across 4 categories (no dominant value)
#   - ~17% missing (above MISSING_VALUE_THRESHOLD=5%, below MISSING_VALUE_IMPUTE_CEILING=20%)
rows = (
    ["active", "inactive", "pending", "closed"] * 5   # 20 non-null, evenly split
    + [None] * 4                                       # 4 nulls → ~17%
)
import random
random.seed(42)
random.shuffle(rows)
df_fixture = pd.DataFrame({"id": range(1, len(rows) + 1), "status": rows, "amount": [100] * len(rows)})

with tempfile.TemporaryDirectory() as tmpdir:
    dataset_dir = pathlib.Path(tmpdir) / "cat_fill_test"
    dataset_dir.mkdir(parents=True)
    # clean_dataset reads CSVs from the top-level folder, clones them into cleaned/
    fixture_path = dataset_dir / "orders.csv"
    df_fixture.to_csv(fixture_path, index=False)

    # Verify rubric flags it
    issues = check_rubric(fixture_path)
    print("Issues found:", issues)
    assert any("status" in i and "Missing values" in i for i in issues), (
        f"expected 'status' to be flagged as missing-values: {issues}"
    )

    result = clean_dataset(str(dataset_dir), llm=None)
    print(result.summary())
    assert any(r.status == "cleaned" for r in result.cleaned_files), (
        f"expected orders.csv to be cleaned: {result}"
    )

    # clean_dataset writes the cleaned file to dataset_dir/cleaned/orders.csv
    cleaned_path = dataset_dir / "cleaned" / "orders.csv"
    cleaned_df = pd.read_csv(cleaned_path)
    print("Filled values:", cleaned_df["status"].value_counts().to_dict())

    # The key assertion: 'Unknown' should appear, not any of the original categories
    # being used exclusively as the fill value (which would indicate mode was forced)
    unique_vals = set(cleaned_df["status"].dropna().unique())
    print("Unique values after cleaning:", unique_vals)
    assert "Unknown" in unique_vals or "unknown" in unique_vals or "Other" in unique_vals or "other" in unique_vals, (
        f"expected 'Unknown' or 'Other' fill value for even-distribution column, got: {unique_vals}"
    )
    print("PASS: even-distribution categorical column was filled with Unknown/Other, not a forced mode.\n")

print(SEP)
print("ALL CATEGORICAL FILL ADVICE ASSERTIONS PASSED")
print(SEP)
