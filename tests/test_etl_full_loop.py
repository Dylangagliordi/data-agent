"""
Full ReAct loop, end to end: a real request that should trigger both tools in sequence.

Uses run_etl_analyst() with a real request to (1) download a real, genuinely messy CSV
from a public URL into a folder, then (2) clean that folder. The approval gate inside
transform_load's clean_dataset() call still fires for real — this test pipes a real
'yes' via stdin so the whole loop can complete without hanging on a human.

Run with:
    printf 'yes\\nyes\\nyes\\n' | uv run python -m tests.test_etl_full_loop
"""

import shutil
from pathlib import Path

from agents.etl_analyst import run_etl_analyst

OUTPUT_FOLDER = "data/_test_etl/e2e_download"
shutil.rmtree(OUTPUT_FOLDER, ignore_errors=True)

request = (
    "Download this file: "
    "https://raw.githubusercontent.com/Jcharis/Data-Cleaning-Practical-Examples/master/unclean_data.csv "
    f"into the folder {OUTPUT_FOLDER} as csv. Then check that folder for any data quality "
    "issues and clean it if needed."
)

print("REQUEST:", request)
print("=" * 70)

final_answer = run_etl_analyst(request)

print("\n" + "=" * 70)
print("FINAL ANSWER:")
print(final_answer)

downloaded = list(Path(OUTPUT_FOLDER).glob("*.csv"))
assert downloaded, f"expected a downloaded csv in {OUTPUT_FOLDER}, found none"
print(f"\nPASS: file was actually downloaded to {OUTPUT_FOLDER}: {[p.name for p in downloaded]}")

cleaned_dir = Path(OUTPUT_FOLDER) / "cleaned"
assert cleaned_dir.exists(), (
    f"expected a cleaned/ folder at {cleaned_dir} — the messy fixture has real issues "
    "(missing values, duplicate rows, encoding artifacts) so transform_load should have "
    "flagged and cleaned it"
)
print(f"PASS: cleaned/ folder exists at {cleaned_dir} — transform_load actually ran cleaning.")

assert not final_answer.startswith("Stopped after"), (
    f"expected a real completion, not a recursion-limit bailout: {final_answer}"
)
print("PASS: completed within the step limit (not a recursion-limit bailout).")

print("\nALL END-TO-END REACT LOOP ASSERTIONS PASSED")
