"""Standalone test for the extract_load tool: a real download from a real public URL,
and a deliberately bad URL producing a clear error rather than a crash.

extract_load is a @tool-decorated function; .invoke() (not calling it directly) is the
correct way to exercise it the same way the ReAct graph's ToolNode would.
"""

import shutil
from pathlib import Path

from agents.etl_analyst import extract_load

OUTPUT_FOLDER = "data/_test_etl/extract_download"

print("=" * 70)
print("CASE 1: real download from a real public URL")
print("=" * 70)

shutil.rmtree(OUTPUT_FOLDER, ignore_errors=True)

result1 = extract_load.invoke(
    {
        "url": "https://raw.githubusercontent.com/pandas-dev/pandas/main/README.md",
        "output_folder": OUTPUT_FOLDER,
        "format": "md",
    }
)
print(result1)
assert result1.startswith("Downloaded "), f"expected a success message, got: {result1}"
assert not result1.startswith("ERROR"), f"expected success, got an error: {result1}"

downloaded_files = list(Path(OUTPUT_FOLDER).glob("*"))
assert len(downloaded_files) == 1, f"expected exactly 1 downloaded file, got: {downloaded_files}"
downloaded_path = downloaded_files[0]
assert downloaded_path.stat().st_size > 0, "downloaded file is empty"
print(f"PASS: real file downloaded to {downloaded_path} ({downloaded_path.stat().st_size} bytes)")

print("\n" + "=" * 70)
print("CASE 2: deliberately bad URL")
print("=" * 70)

result2 = extract_load.invoke(
    {
        "url": "https://this-domain-does-not-exist-xyz123456789.invalid/file.csv",
        "output_folder": OUTPUT_FOLDER,
        "format": "csv",
    }
)
print(result2)
assert result2.startswith("ERROR"), f"expected a clear error message, got: {result2}"
print("PASS: bad URL produced a clear error string, did not crash.")

print("\n" + "=" * 70)
print("CASE 3: URL that resolves but returns a non-200 status")
print("=" * 70)

result3 = extract_load.invoke(
    {
        "url": "https://raw.githubusercontent.com/this-repo-does-not-exist-xyz/nope/main/nope.csv",
        "output_folder": OUTPUT_FOLDER,
        "format": "csv",
    }
)
print(result3)
assert result3.startswith("ERROR"), f"expected a clear error message for a non-200 status, got: {result3}"
print("PASS: non-200 response produced a clear error string, did not crash.")

print("\nALL extract_load ASSERTIONS PASSED")
