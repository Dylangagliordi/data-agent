"""Standalone test: _clean_issue_group's row-count-integrity check for
ROW_COUNT_INTEGRITY_PREFIXES issues (currently "Column misalignment:" and
"Structural issue:").

Regression test for a real incident: cleaning data/data-science-jobs/Uncleaned_DS_jobs.csv
generated a "fix" for "Column misalignment" that made the flagged unmatched-quote
condition disappear (so check_rubric's plain textual re-check reported the issue as
gone) while actually splitting rows with embedded newlines into extra bogus rows —
672 real rows became 778 malformed ones. That corrupted file then passed clean_dataset()
as "resolved" and only failed much later, at the Postgres load step, with a far less
diagnosable "invalid input syntax for type bigint" error.

Uses the misalign_row_corruption/ fixture (exactly one fail-level issue: "Column
misalignment:", zero warn-level issues) and a fake LLM that deliberately reproduces
the incident: attempt 1 returns code that resolves the textual quote-count check but
splits the file's 3 real rows into 4 by injecting a spurious extra newline; attempt 2
returns a real, row-count-preserving fix (append a closing quote to any row with an
odd quote count, done via a real CSV-aware row splitter so it doesn't rely on
newline-based line splitting).

Confirms:
- attempt 1's "fix" is correctly rejected specifically for its row-count change (not
  merely retried as if the quote issue were still textually present) — the retry
  prompt fed back to attempt 2 mentions the row-count mismatch explicitly.
- attempt 2's real fix is accepted, and the group's final status is "resolved" with
  the file's row count intact.

Run with:
    printf 'yes\\nyes\\n' | uv run python -m tests.test_data_cleaning_row_count_integrity
"""

import shutil
from pathlib import Path

from utils.data_cleaning import _clean_issue_group, _count_csv_rows, _clone_file

FOLDER = Path("data/_test_etl/misalign_row_corruption")
RAW_FILE = FOLDER / "orders.csv"
CLEANED_DIR = FOLDER / "cleaned"
CLEANED_FILE = CLEANED_DIR / "orders.csv"

ISSUE = "Column misalignment: 2 row(s) contain an unmatched quote character, which can cause fields to be misread or misaligned."


class FakeResponse:
    def __init__(self, content):
        self.content = content


class CorruptThenFixLLM:
    """Attempt 1: makes the textual unmatched-quote condition disappear by splitting
    one row into two extra (bogus) rows — the real bug this test guards against.
    Attempt 2: a real fix that preserves every original row.
    """

    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        human_content = messages[1][1]
        self.calls.append(human_content)
        if len(self.calls) == 1:
            # Deliberately corrupt: balances quote counts by injecting a stray newline
            # that splits row 3 into two rows, growing the file from 3 to 4 data rows.
            return FakeResponse(
                "path = " + repr(str(CLEANED_FILE)) + "\n"
                "with open(path, 'r', encoding='utf-8') as f:\n"
                "    content = f.read()\n"
                "content = content.replace('another \"quote row', 'another \"\\nquote row\"')\n"
                "with open(path, 'w', encoding='utf-8') as f:\n"
                "    f.write(content)\n"
            )
        # Real fix: rewrite the file from a known-good, properly-quoted state (a real
        # model, told the previous attempt corrupted the row count, would regenerate a
        # correct fix from the file's actual current content rather than compounding
        # the previous attempt's damage — this fixture keeps that deterministic by
        # writing the correct target output directly).
        return FakeResponse(
            "path = " + repr(str(CLEANED_FILE)) + "\n"
            "content = (\n"
            "    'id,description\\n'\n"
            "    '1,say \"\"hi there\\n'\n"
            "    '2,normal row\\n'\n"
            "    '3,another \"\"quote row\\n'\n"
            ")\n"
            "with open(path, 'w', encoding='utf-8', newline='') as f:\n"
            "    f.write(content)\n"
        )


print("=" * 70)
print("Row-count-integrity check: reject a textually-clean but row-corrupting fix")
print("=" * 70)

# Fresh clone every run so re-running this test file is idempotent.
if CLEANED_DIR.exists():
    shutil.rmtree(CLEANED_DIR)
cloned_path = _clone_file(RAW_FILE, CLEANED_DIR)

row_count_before = _count_csv_rows(cloned_path)
assert row_count_before == 3, f"expected fixture to start with 3 data rows, got {row_count_before}"

fake_llm = CorruptThenFixLLM()
status, attempts, error, remaining, code = _clean_issue_group(cloned_path, [ISSUE], fake_llm)

print(f"status={status} attempts={attempts}")
print(f"error={error!r}")

assert len(fake_llm.calls) == 2, f"expected exactly 2 generation calls, got {len(fake_llm.calls)}"
assert "row count" in fake_llm.calls[1].lower() or "row_count" in fake_llm.calls[1].lower() or \
    "row count changed" in fake_llm.calls[1], (
    "expected attempt 2's prompt to be told about the row-count mismatch from attempt 1, "
    f"got: {fake_llm.calls[1]!r}"
)
print("PASS: attempt 2 was told the row count changed, not just that the issue text reappeared.")

assert status == "resolved", f"expected the group to end resolved (via the real attempt-2 fix), got {status}"
assert attempts == 2, f"expected exactly 2 attempts (1 rejected for row corruption, 1 real fix), got {attempts}"

row_count_after = _count_csv_rows(cloned_path)
assert row_count_after == row_count_before, (
    f"expected final row count to match the original ({row_count_before}), got {row_count_after} — "
    "the row-count-integrity check should never let a row-corrupting result stand as 'resolved'"
)
print(f"PASS: final row count ({row_count_after}) matches the original ({row_count_before}).")

print("\nALL ROW-COUNT-INTEGRITY ASSERTIONS PASSED")
