"""Standalone test for the 17 additional deterministic rubric categories added on top
of the original 8 (see test_data_cleaning_rubric.py for those) — no LLM, no approval
gate, just the rubric logic against real fixture files under
data/_test_etl/rubric_v2/.

For each new category: one deliberately-flawed fixture confirming it's detected, and
(where meaningfully distinct) one clean fixture confirming no false positive.
"""

from utils.data_cleaning import check_rubric

D = "data/_test_etl/rubric_v2"

# (flawed_file, clean_file_or_None, keyword expected in the matching issue string)
CASES = [
    ("placeholder_flawed.csv", "placeholder_clean.csv", "Placeholder"),
    ("boolean_flawed.csv", "boolean_clean.csv", "boolean"),
    ("zip_flawed.csv", "zip_clean.csv", "leading zero"),
    ("currency_flawed.csv", "currency_clean.csv", "Currency"),
    ("locale_flawed.csv", "locale_clean.csv", "Locale"),
    ("floatnoise_flawed.csv", "floatnoise_clean.csv", "floating-point"),
    ("control_flawed.csv", "control_clean.csv", "printable"),
    ("spreadsheet_flawed.csv", "spreadsheet_clean.csv", "Spreadsheet"),
    ("delimiters_flawed.csv", "delimiters_clean.csv", "delimiters"),
    ("header_flawed.csv", "header_clean.csv", "header"),
    ("special_header_flawed.csv", "special_header_clean.csv", "Special char"),
    ("quote_flawed.csv", "quote_clean.csv", "misalignment"),
    ("header_dup_flawed.csv", None, "duplicated mid-file"),
    ("trailing_flawed.csv", "trailing_clean.csv", "Trailing"),
    ("bom_flawed.csv", "bom_clean.csv", "Byte-order"),
    ("granularity_flawed.csv", "granularity_clean.csv", "granularity"),
    ("dangling_flawed.csv", "dangling_clean.csv", "Dangling"),
]

assert len(CASES) == 17, f"expected exactly 17 new categories under test, got {len(CASES)}"

for flawed, clean, keyword in CASES:
    print("=" * 70)
    print(f"{flawed} (expect: '{keyword}' issue)")
    print("=" * 70)
    issues = check_rubric(f"{D}/{flawed}")
    for i in issues:
        print(" -", i)
    matched = [i for i in issues if keyword.lower() in i.lower()]
    assert matched, f"expected an issue containing '{keyword}' for {flawed}, got: {issues}"
    print(f"PASS: {flawed} correctly flagged for '{keyword}'.")

    if clean:
        clean_issues = check_rubric(f"{D}/{clean}")
        clean_matched = [i for i in clean_issues if keyword.lower() in i.lower()]
        assert not clean_matched, (
            f"false positive: {clean} was flagged for '{keyword}': {clean_matched}"
        )
        print(f"PASS: {clean} correctly produced no '{keyword}' false positive.")
    print()

print("=" * 70)
print(f"ALL {len(CASES)} NEW RUBRIC CATEGORY ASSERTIONS PASSED")
print("=" * 70)
