"""Standalone test for Part 2: strengthened missing-value / invalid-value reasoning in
the cleaning-code-generation prompt (utils/data_cleaning.py).

Case 1 (deterministic, no LLM): _issue_guidance() parses the real percentage straight out
of a "Missing values" issue string and returns the correct proportional guidance text
(impute-ok under the 20% ceiling, don't-impute at/above it) — and returns nothing for a
non-missing-values issue (e.g. "Invalid values"), confirming the guidance is scoped to
the one category it's meant for.

Cases 2-5 (real LLM, one clean_dataset() call covering all three fixture files in
data/_test_etl/part2/ together):
- missing_low.csv (~15% missing): expect real imputation, no nulls left.
- missing_high.csv (~40% missing): expect NOT blind uniform imputation.
- impossible_and_unusual.csv: a genuinely impossible negative amount (still corrected,
  regression check) alongside an unusual-but-plausible bonus_amount that is NOT itself
  flagged as an issue (expect it left completely untouched).

Run with:
    printf 'yes\\nyes\\nyes\\n' | uv run python -m tests.test_data_cleaning_missing_invalid_reasoning
"""

import pandas as pd

from utils.data_cleaning import (
    MISSING_VALUE_IMPUTE_CEILING,
    _issue_guidance,
    check_rubric,
    clean_dataset,
)

D = "data/_test_etl/part2"

print("=" * 70)
print("CASE 1: _issue_guidance() — deterministic, no LLM")
print("=" * 70)

low_guidance = _issue_guidance("Missing values: column 'amount' is 15.0% missing/blank (threshold 5%).")
print("15% guidance:", low_guidance)
assert "15.0%" in low_guidance
assert "imputing is reasonable" in low_guidance
assert f"{MISSING_VALUE_IMPUTE_CEILING:.0%}" in low_guidance

high_guidance = _issue_guidance("Missing values: column 'amount' is 40.0% missing/blank (threshold 5%).")
print("40% guidance:", high_guidance)
assert "40.0%" in high_guidance
assert "do NOT silently impute" in high_guidance

other_guidance = _issue_guidance("Invalid values: column 'amount' has 1 negative value(s).")
print("non-missing-values guidance:", repr(other_guidance))
assert other_guidance == "", "guidance should only fire for Missing values issues"
print("PASS: _issue_guidance produces correct, real-percentage-driven guidance.\n")


print("=" * 70)
print("Pre-check: confirm the rubric flags each fixture the way this test expects")
print("=" * 70)
issues_low = check_rubric(f"{D}/missing_low.csv")
issues_high = check_rubric(f"{D}/missing_high.csv")
issues_imp = check_rubric(f"{D}/impossible_and_unusual.csv")
print("missing_low.csv:", issues_low)
print("missing_high.csv:", issues_high)
print("impossible_and_unusual.csv:", issues_imp)
assert any("15.0%" in i for i in issues_low), f"expected ~15% missing: {issues_low}"
assert any("40.0%" in i for i in issues_high), f"expected ~40% missing: {issues_high}"
assert any("Invalid values" in i and "negative" in i for i in issues_imp)
assert not any("bonus_amount" in i for i in issues_imp), (
    "test setup assumption broken: bonus_amount should not itself be flagged"
)
print()

print("=" * 70)
print("Running clean_dataset() once against all three fixtures together (real LLM)")
print("=" * 70)
result = clean_dataset(D, llm=None)  # uses real pick_llm("high")
print(result.summary())

by_name = {r.file_name: r for r in result.cleaned_files + result.skipped_files}
for name in ("missing_low.csv", "missing_high.csv", "impossible_and_unusual.csv"):
    rec = by_name.get(name)
    assert rec is not None and rec.status == "cleaned", f"expected {name} cleaned, got: {rec}"
print("PASS: all three fixtures cleaned successfully.\n")

print("=" * 70)
print("CASE 2: ~15% missing — expect real imputation, no nulls left")
print("=" * 70)
cleaned_low = pd.read_csv(f"{D}/cleaned/missing_low.csv")
assert cleaned_low["amount"].isna().sum() == 0, (
    f"expected no remaining nulls after imputation, got {cleaned_low['amount'].isna().sum()}"
)
print("PASS: ~15% missing column was actually imputed (no nulls left in real output).\n")

print("=" * 70)
print("CASE 3: ~40% missing — expect NOT blind uniform imputation")
print("=" * 70)
cleaned_high = pd.read_csv(f"{D}/cleaned/missing_high.csv")
if "amount" not in cleaned_high.columns:
    print("Column 'amount' was dropped entirely — an accepted conservative strategy.")
else:
    remaining_nulls = cleaned_high["amount"].isna().sum()
    if remaining_nulls > 0:
        print(f"{remaining_nulls} value(s) left null rather than imputed — conservative, as expected.")
    else:
        assert len(cleaned_high) < 20, (
            "column has no nulls and row count is unchanged — looks like every null was "
            "silently imputed uniformly, which is exactly what the 40%-missing rule forbids"
        )
        print(f"Rows with missing amounts were dropped instead of imputed (from 20 to {len(cleaned_high)}).")
print("PASS: ~40% missing column was NOT blindly, uniformly imputed.\n")

print("=" * 70)
print("CASE 4: genuinely impossible value (negative amount) — still corrected (regression)")
print("=" * 70)
cleaned_imp = pd.read_csv(f"{D}/cleaned/impossible_and_unusual.csv")
assert (cleaned_imp["amount"].dropna() < 0).sum() == 0, (
    f"expected no negative amounts left, got: {cleaned_imp['amount'].tolist()}"
)
print("PASS: genuinely impossible negative value was corrected (no regression).\n")

print("=" * 70)
print("CASE 5: unusual-but-plausible value NOT in the issues list — left untouched")
print("=" * 70)
original_bonus = [100.00, 200.00, 150.00, 999999.00, 175.00]
cleaned_bonus = cleaned_imp["bonus_amount"].tolist()
assert cleaned_bonus == original_bonus, (
    f"bonus_amount was altered even though it wasn't a listed issue: {cleaned_bonus} != {original_bonus}"
)
print("PASS: unusual-but-unflagged bonus_amount values (including 999999.00) left untouched.\n")

print("=" * 70)
print("ALL MISSING-VALUE / INVALID-VALUE REASONING ASSERTIONS PASSED")
print("=" * 70)
