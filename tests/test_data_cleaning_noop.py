"""Standalone test: clean_dataset() against a genuinely clean file — confirms it does
nothing (no cloning, no approval prompt) for that file.

clean_dataset() now also runs the exploratory discovery phase (explore_and_verify) on
every file, including this one — but this fixture's columns have only 5 rows each,
under _EXPLORE_MIN_COLUMN_ROWS, so explore_column skips them without ever calling the
LLM (see utils/data_cleaning.py). No stdin piping needed here: if this test hangs
waiting on input(), that itself is a failure (it would mean the approval gate fired
for a file that should never have reached it).
"""

from pathlib import Path

from utils.data_cleaning import clean_dataset

FOLDER = "data/_test_etl/clean_only"

result = clean_dataset(FOLDER)

print(result.summary())

assert result.untouched_files == ["products_clean.csv"], (
    f"expected products_clean.csv untouched, got: {result.untouched_files}"
)
assert result.cleaned_files == [], f"expected no cleaned files, got: {result.cleaned_files}"
assert result.skipped_files == [], f"expected no skipped files, got: {result.skipped_files}"
print("PASS: clean file correctly left untouched (no cleaning attempted).")

cleaned_dir = Path(result.cleaned_dir)
assert not cleaned_dir.exists(), (
    f"expected no cleaned/ folder to be created at all (nothing needed cleaning), "
    f"but found: {cleaned_dir}"
)
print("PASS: no cleaned/ folder was created — no cloning happened for this file.")

print("\nALL CLEAN-FILE (NO-OP) ASSERTIONS PASSED")
