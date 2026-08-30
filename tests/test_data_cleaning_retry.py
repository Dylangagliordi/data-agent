"""Standalone test: clean_dataset() retry-on-real-execution-error path.

Injects a fake LLM (not pick_llm) via clean_dataset's llm= parameter, so this test does
NOT depend on the real model happening to write broken code — it deterministically forces
a real execution failure on attempt 1, then a working fix on attempt 2, and confirms:
- attempt 1's real exec() error is fed back into the second generation call (visible in
  the fake LLM's recorded call history).
- the file ends up cleaned successfully in 2 attempts.

A second run forces ALL attempts to fail (guaranteed-broken code every time) and confirms
it stops at MAX_CLEAN_ATTEMPTS (3), reports the real last error, and skips the file rather
than raising or hanging.

Approval is auto-approved for every attempt via piped stdin ('yes' repeated) — this test
is about the retry loop, not the approval gate (already covered by
test_data_cleaning_approve.py / test_data_cleaning_decline.py).

Run with:
    printf 'yes\\nyes\\nyes\\nyes\\nyes\\n' | uv run python -m tests.test_data_cleaning_retry
"""

from pathlib import Path

from utils.data_cleaning import MAX_CLEAN_ATTEMPTS, clean_dataset


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FailThenSucceedLLM:
    """First .invoke() call returns code that raises a real Python error when exec'd.
    Second call returns code that actually succeeds. Records every call's human message
    so the test can confirm the real error text was actually included in the retry call."""

    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        human_content = messages[1][1]
        self.calls.append(human_content)
        if len(self.calls) == 1:
            return FakeResponse("raise RuntimeError('deliberate forced failure for the retry test')")
        return FakeResponse(
            "import pandas as pd\n"
            f"path = {str(RETRY_FILE)!r}\n"
            "df = pd.read_csv(path, dtype=str)\n"
            "df.to_csv(path, index=False)\n"
        )


class AlwaysFailLLM:
    """Every call returns code guaranteed to raise, no matter how many times it retries."""

    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages[1][1])
        return FakeResponse("raise RuntimeError('this will never succeed, attempt logged')")


FOLDER = "data/_test_etl/retry"
RETRY_FILE = Path(FOLDER) / "orders_missing.csv"

print("=" * 70)
print("CASE 1: fails once, then succeeds on retry")
print("=" * 70)

fake_llm = FailThenSucceedLLM()
result1 = clean_dataset(FOLDER, llm=fake_llm)
print(result1.summary())

assert len(fake_llm.calls) == 2, f"expected exactly 2 generation calls, got {len(fake_llm.calls)}"
assert "deliberate forced failure" in fake_llm.calls[1], (
    "expected the real error from attempt 1 to be fed into the attempt-2 prompt"
)
print("PASS: real error from attempt 1 was included in the attempt-2 prompt.")

assert len(result1.cleaned_files) == 1, f"expected 1 cleaned file, got {result1.cleaned_files}"
assert result1.cleaned_files[0].attempts == 2, (
    f"expected it to succeed on attempt 2, got attempts={result1.cleaned_files[0].attempts}"
)
print("PASS: file cleaned successfully on attempt 2 after a real forced failure on attempt 1.")


print("\n" + "=" * 70)
print(f"CASE 2: fails every time, capped at {MAX_CLEAN_ATTEMPTS} attempts")
print("=" * 70)

always_fail_llm = AlwaysFailLLM()
result2 = clean_dataset(FOLDER, llm=always_fail_llm)
print(result2.summary())

assert len(always_fail_llm.calls) == MAX_CLEAN_ATTEMPTS, (
    f"expected exactly {MAX_CLEAN_ATTEMPTS} generation calls, got {len(always_fail_llm.calls)}"
)
assert len(result2.cleaned_files) == 0
assert len(result2.skipped_files) == 1
skipped = result2.skipped_files[0]
assert skipped.status == "skipped_failed", f"expected status skipped_failed, got {skipped.status}"
assert skipped.attempts == MAX_CLEAN_ATTEMPTS
assert "this will never succeed" in skipped.error, (
    f"expected the real last error in the skipped record: {skipped.error}"
)
print(f"PASS: capped at {MAX_CLEAN_ATTEMPTS} attempts, file skipped with the real last error reported.")

print("\nALL RETRY-PATH ASSERTIONS PASSED")
