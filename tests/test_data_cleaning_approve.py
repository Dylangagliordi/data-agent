"""Standalone test: clean_dataset() approval gate, APPROVE path.

Uses a real LLM call (pick_llm("high")) to generate real cleaning code against a real
messy fixture file, and feeds the approval gate's input() a real 'yes' via piped stdin.
Run with stdin piped, e.g.:

    printf 'yes\\n' | uv run python -m tests.test_data_cleaning_approve

Confirms:
- The raw source file is never modified (checksum before/after).
- Exactly one file ends up in result.cleaned_files with status "cleaned".
- A cleaned/ folder was created with the (now-modified) clone.
"""

import hashlib
from pathlib import Path

from utils.data_cleaning import clean_dataset

FOLDER = "data/_test_etl/approve_yes"
RAW_FILE = Path(FOLDER) / "orders_missing.csv"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


raw_hash_before = sha256(RAW_FILE)

result = clean_dataset(FOLDER)

print(result.summary())

raw_hash_after = sha256(RAW_FILE)
assert raw_hash_before == raw_hash_after, "RAW FILE WAS MODIFIED — this must never happen"
print("\nPASS: raw file untouched (hash identical before/after).")

assert len(result.cleaned_files) == 1, f"expected 1 cleaned file, got {len(result.cleaned_files)}"
assert result.cleaned_files[0].status == "cleaned"
assert result.cleaned_files[0].file_name == "orders_missing.csv"
print("PASS: orders_missing.csv was cleaned (approved) with status 'cleaned'.")

cleaned_path = Path(result.cleaned_dir) / "orders_missing.csv"
assert cleaned_path.exists(), f"expected cleaned output at {cleaned_path}"
print(f"PASS: cleaned output exists at {cleaned_path}")

print("\nALL APPROVE-PATH ASSERTIONS PASSED")
