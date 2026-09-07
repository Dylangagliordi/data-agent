"""
Spec 1, Part 2: fix duplicate-row detection being structurally blind to a raw
leading row-index column.

The reference Kaggle notebook for this project's dataset loads with
index_col="index", excluding the raw leading index column before calling
.duplicated(). This project's loader (_read_csv_robust) keeps that column as
ordinary string data, so every row was artificially unique by construction —
_check_duplicates found 0 duplicates on Uncleaned_DS_jobs.csv where the
reference notebook found 13.

Fix: _looks_like_row_index_column (name looks index-like AND
_is_sequential_id_like_column) excludes an obvious row-index column from
_check_duplicates' comparison only — the DataFrame/stored table itself is
untouched.

Test 1: a synthetic fixture with an 'index' column (0..n-1) and two genuinely
duplicate data rows — confirms the duplicate is now found, and confirms the
'index' column itself is NOT dropped from the DataFrame (only excluded from
the comparison).

Test 2: a genuine data column literally named 'index' whose VALUES are not
sequential (real, non-index data) must NOT be excluded — name alone is never
enough.

Test 3: live run against the real data/data-science-jobs/Uncleaned_DS_jobs.csv
— confirms exactly 13 duplicate rows are found, matching the reference
notebook.

Test 4: an 'Unnamed: 0' style index column (pandas' own default when a CSV is
saved with index=True) is excluded the same way.
"""

import pandas as pd

import utils.data_cleaning as dc

print("=" * 70)
print("TEST 1: raw 'index' column no longer hides real duplicate rows")
print("=" * 70)

df1 = pd.DataFrame({
    "index": ["0", "1", "2", "3"],
    "name": ["alice", "bob", "alice", "carol"],
    "amount": ["10", "20", "10", "30"],
})
issues1 = dc._check_duplicates(df1)
assert any(i.startswith("Duplicate rows: 1 ") for i in issues1), (
    f"expected exactly 1 duplicate row (rows 0 and 2 are identical except for 'index'), got {issues1}"
)
assert list(df1.columns) == ["index", "name", "amount"], (
    "the 'index' column must remain in the real DataFrame — only excluded from the comparison"
)
print("PASS: duplicate row found despite the artificially-unique 'index' column; "
      "DataFrame itself untouched.\n")

print("=" * 70)
print("TEST 2: a column literally named 'index' with non-sequential real data")
print("is NOT excluded — name alone is not enough")
print("=" * 70)

df2 = pd.DataFrame({
    "index": ["west", "east", "south", "north"],
    "amount": ["10", "20", "10", "30"],
})
assert not dc._looks_like_row_index_column("index", df2["index"]), (
    "a non-sequential 'index' column holding real data must not be treated as a row index"
)
issues2 = dc._check_duplicates(df2)
assert issues2 == [], (
    f"no two full rows are identical once the real 'index' values are counted, got {issues2}"
)
print("PASS: a same-named but non-sequential column is correctly left in the comparison.\n")

print("=" * 70)
print("TEST 3: live run against the real Uncleaned_DS_jobs.csv")
print("=" * 70)

real_path = "data/data-science-jobs/Uncleaned_DS_jobs.csv"
real_df = dc._read_csv_robust(real_path)
real_issues = dc._check_duplicates(real_df)
dup_issue = next((i for i in real_issues if i.startswith("Duplicate rows:")), None)
assert dup_issue is not None, f"expected a 'Duplicate rows:' issue, got {real_issues}"
assert "13 fully duplicate rows" in dup_issue, (
    f"expected 13 duplicate rows (matching the reference notebook), got: {dup_issue}"
)
print(f"PASS: {dup_issue}\n")

print("=" * 70)
print("TEST 4: 'Unnamed: 0' style index column is excluded the same way")
print("=" * 70)

df4 = pd.DataFrame({
    "Unnamed: 0": ["0", "1", "2"],
    "city": ["ny", "sf", "ny"],
})
issues4 = dc._check_duplicates(df4)
assert any(i.startswith("Duplicate rows: 1 ") for i in issues4), (
    f"expected 1 duplicate row, got {issues4}"
)
print("PASS: 'Unnamed: 0' index column correctly excluded from the comparison.\n")

print("=" * 70)
print("ALL DUPLICATE-ROW-INDEX-EXCLUSION (SPEC 1, PART 2) ASSERTIONS PASSED")
print("=" * 70)
