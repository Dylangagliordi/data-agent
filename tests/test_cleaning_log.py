"""Test: clean_dataset() appends a real entry to logs/cleaning_log.jsonl.

Uses the existing clean-file fixture (no issues, no LLM call, no approval
prompt needed) — verifies the log entry is written with correct structure
and fields regardless of whether any files were actually cleaned.
"""

import json
from pathlib import Path

from utils.data_cleaning import clean_dataset

LOG_PATH = Path("logs/cleaning_log.jsonl")
FOLDER = "data/_test_etl/clean_only"

# Record how many lines exist before the run.
before_count = 0
if LOG_PATH.exists():
    before_count = sum(1 for _ in LOG_PATH.open())

print("=" * 70)
print("TEST: cleaning_log.jsonl entry written by clean_dataset()")
print("=" * 70)

result = clean_dataset(FOLDER, trigger="manual")
print("clean_dataset() returned. Summary:")
print(result.summary())

# Verify a new entry was appended.
assert LOG_PATH.exists(), f"cleaning_log.jsonl must exist after clean_dataset() runs"
after_lines = LOG_PATH.read_text().splitlines()
assert len(after_lines) == before_count + 1, (
    f"expected exactly one new line appended (was {before_count}, now {len(after_lines)})"
)

entry = json.loads(after_lines[-1])
print("\nLast log entry:")
print(json.dumps(entry, indent=2))

# Structural checks.
assert "timestamp" in entry, "entry must have a timestamp"
assert entry["source_folder"].endswith("clean_only"), (
    f"source_folder must point at the folder, got: {entry['source_folder']!r}"
)
assert entry["trigger"] == "manual", f"trigger must be 'manual', got: {entry['trigger']!r}"
assert "files" in entry, "entry must have a 'files' key"
# Clean-only folder: no files processed (all untouched), so files list is empty.
assert entry["files"] == [], (
    f"files must be [] for a no-op run (all files clean), got: {entry['files']}"
)
print("\nPASS: log entry written with correct structure and fields.")

# Now test that trigger="auto_redirect" is also accepted.
print("\n" + "=" * 70)
print("TEST: trigger='auto_redirect' written correctly")
print("=" * 70)
clean_dataset(FOLDER, trigger="auto_redirect")
auto_entry = json.loads(LOG_PATH.read_text().splitlines()[-1])
assert auto_entry["trigger"] == "auto_redirect", (
    f"trigger must be 'auto_redirect', got: {auto_entry['trigger']!r}"
)
print(f"PASS: trigger='auto_redirect' written correctly.")

print("\n" + "=" * 70)
print("ALL CLEANING LOG TESTS PASSED")
print("=" * 70)
