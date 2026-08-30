"""Standalone test for the transform_load tool: confirms it's a real thin wrapper around
clean_dataset() — same approval gate applies (fed via piped stdin), same behavior.

Run with:
    printf 'yes\\n' | uv run python -m tests.test_transform_load
"""

import hashlib
from pathlib import Path

from agents.etl_analyst import transform_load

FOLDER = "data/_test_etl/tool_mixed"
RAW_FILE = Path(FOLDER) / "orders_missing.csv"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


raw_hash_before = sha256(RAW_FILE)

result = transform_load.invoke({"folder_path": FOLDER})
print(result)

assert "orders_missing.csv" in result
assert "products_clean.csv" in result
assert "Untouched" in result and "Cleaned successfully" in result
print("\nPASS: transform_load returned clean_dataset's real summary string.")

raw_hash_after = sha256(RAW_FILE)
assert raw_hash_before == raw_hash_after, "RAW FILE WAS MODIFIED via the tool — must never happen"
print("PASS: raw file untouched via the tool path too.")

cleaned_path = Path(FOLDER) / "cleaned" / "orders_missing.csv"
assert cleaned_path.exists(), f"expected cleaned output at {cleaned_path}"
print(f"PASS: cleaned output exists at {cleaned_path}")

print("\nALL transform_load ASSERTIONS PASSED")
