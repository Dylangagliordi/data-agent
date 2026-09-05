"""
Regression test for architecture review point #28 (SSRF protection):
extract_load used to fetch any URL an LLM decided to pass it, with no
allowlist and no blocking of private-IP or cloud-metadata endpoints (e.g.
169.254.169.254, which serves IAM credentials on AWS/GCP/Azure).

Tests _validate_fetch_url directly (unit, no network) for the IP-range/
metadata-hostname decisions, then confirms extract_load itself refuses a
private-IP/metadata-endpoint URL BEFORE any request is made (no file written,
no exception raised — just a clear ERROR string), and that a real, legitimate
public URL still works (regression guard against over-blocking).
"""

import shutil
from pathlib import Path

from agents.etl_analyst import _validate_fetch_url, extract_load

print("=" * 70)
print("UNIT: _validate_fetch_url rejects private/internal/metadata targets")
print("=" * 70)

blocked_cases = [
    ("http://169.254.169.254/latest/meta-data/", "AWS/GCP/Azure metadata IP (link-local)"),
    ("http://169.254.169.254/computeMetadata/v1/", "GCP metadata IP"),
    ("http://metadata.google.internal/computeMetadata/v1/", "GCP metadata hostname"),
    ("http://127.0.0.1:8080/secret", "loopback"),
    ("http://localhost/secret", "loopback via hostname"),
    ("http://10.0.0.5/internal", "private 10.0.0.0/8"),
    ("http://172.16.0.5/internal", "private 172.16.0.0/12"),
    ("http://192.168.1.5/internal", "private 192.168.0.0/16"),
    ("http://[::1]/secret", "IPv6 loopback"),
    ("ftp://example.com/file.csv", "non-http(s) scheme"),
]
for url, why in blocked_cases:
    is_safe, reason = _validate_fetch_url(url)
    assert not is_safe, f"expected {url!r} ({why}) to be BLOCKED, but it was allowed"
    print(f"PASS: blocked {url!r} ({why}) — reason: {reason}")

print()
print("=" * 70)
print("UNIT: _validate_fetch_url allows a real, legitimate public URL")
print("=" * 70)
is_safe, reason = _validate_fetch_url("https://raw.githubusercontent.com/pandas-dev/pandas/main/README.md")
assert is_safe, f"expected a real public URL to be allowed, got blocked: {reason}"
print("PASS: real public URL allowed through validation.\n")


print("=" * 70)
print("TOOL: extract_load refuses a metadata-endpoint URL before any request")
print("=" * 70)

OUTPUT_FOLDER = "data/_test_etl/ssrf_probe"
shutil.rmtree(OUTPUT_FOLDER, ignore_errors=True)

result = extract_load.invoke(
    {
        "url": "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "output_folder": OUTPUT_FOLDER,
        "format": "json",
    }
)
print(result)
assert result.startswith("ERROR: refused to fetch URL"), (
    f"expected a clear refusal before any request, got: {result}"
)
assert not Path(OUTPUT_FOLDER).exists(), (
    "extract_load must not even create the output folder for a blocked URL — "
    "the refusal must happen before any I/O, not after a failed fetch"
)
print("PASS: metadata-endpoint URL refused before any request or file I/O.\n")


print("=" * 70)
print("TOOL: extract_load still works for a real, legitimate public URL")
print("=" * 70)
result2 = extract_load.invoke(
    {
        "url": "https://raw.githubusercontent.com/pandas-dev/pandas/main/README.md",
        "output_folder": OUTPUT_FOLDER,
        "format": "md",
    }
)
print(result2)
assert result2.startswith("Downloaded "), f"expected a real download to still succeed, got: {result2}"
downloaded = list(Path(OUTPUT_FOLDER).glob("*"))
assert len(downloaded) == 1 and downloaded[0].stat().st_size > 0, (
    f"expected exactly one non-empty downloaded file, got: {downloaded}"
)
print(f"PASS: legitimate public URL still downloads normally ({downloaded[0]}).\n")

shutil.rmtree(OUTPUT_FOLDER, ignore_errors=True)

print("=" * 70)
print("ALL extract_load SSRF ASSERTIONS PASSED")
print("=" * 70)
