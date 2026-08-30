"""Standalone test: clean_dataset() approval gate, DECLINE path.

Real LLM call generates real code, but the approval gate is fed a real 'no' via piped
stdin. Confirms the code is genuinely never executed (the clone stays byte-identical to
the raw file — proof no cleaning happened) and the file is reported skipped_declined.

Run with:
    printf 'no\\n' | uv run python -m tests.test_data_cleaning_decline
"""

import hashlib
from pathlib import Path

from utils.data_cleaning import clean_dataset

FOLDER = "data/_test_etl/approve_no"
RAW_FILE = Path(FOLDER) / "orders_missing.csv"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


raw_hash_before = sha256(RAW_FILE)

result = clean_dataset(FOLDER)

print(result.summary())

raw_hash_after = sha256(RAW_FILE)
assert raw_hash_before == raw_hash_after, "RAW FILE WAS MODIFIED — this must never happen"
print("\nPASS: raw file untouched (hash identical before/after).")

assert len(result.cleaned_files) == 0, f"expected 0 cleaned files, got {len(result.cleaned_files)}"
assert len(result.skipped_files) == 1, f"expected 1 skipped file, got {len(result.skipped_files)}"
assert result.skipped_files[0].status == "skipped_declined"
print("PASS: orders_missing.csv correctly skipped with status 'skipped_declined'.")

# The clone was made (that's expected — cloning happens before approval), but it must be
# byte-identical to the raw file since the declined code was never executed against it.
clone_path = Path(result.cleaned_dir) / "orders_missing.csv"
assert clone_path.exists(), f"expected the clone to exist at {clone_path} (pre-approval clone)"
clone_hash = sha256(clone_path)
assert clone_hash == raw_hash_before, (
    "clone was modified even though approval was declined — the code must have run anyway"
)
print("PASS: clone exists but is byte-identical to raw (declined code never executed).")

print("\nALL DECLINE-PATH ASSERTIONS PASSED")
