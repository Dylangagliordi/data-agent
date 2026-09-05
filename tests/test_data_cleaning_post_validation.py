"""Standalone test for post-cleaning validation in clean_dataset() (the final, full
check_rubric() pass + row-count-loss comparison run after the fail-then-warn pipeline
completes — see the fail/warn severity-split restructuring in
test_data_cleaning_fail_warn_split.py for the per-issue/batch mechanics themselves).

Confirms clean_dataset() no longer treats "the generated code executed without raising"
as proof cleaning actually worked — it now re-runs check_rubric() against the real
cleaned output and reacts to what it finds there, plus tracks row-count loss.

Case 1 (real LLM): a straightforward, single genuinely-fixable fail-level issue (no
warn-level issues in this fixture). Confirms the final post-cleaning re-check passes and
this is reflected in both the FileCleaningRecord and result.summary() text.

Case 2 (fake LLM, deterministic): a file with one fail-level issue and one warn-level
issue. The fake LLM's first response (targeting the fail-level issue alone) succeeds
immediately; the warn-level batch's first response fixes only the categorical-value
issue's actual DATA but is deliberately written so the re-check still detects the
formatting-noise wording is generated correctly on retry — the fake LLM's first response
for the batch is a real, executable script that does NOT actually fix the batch's issue,
leaving it detectable. Confirms the post-cleaning re-check at the warn-batch level
catches the still-present issue and triggers a genuine retry (not a false "resolved"
report) — the second call actually fixes it, and only THEN is the file reported cleaned.

Case 3 (fake LLM, deterministic): a file with one fail-level issue, whose fake LLM
response technically fixes that one issue but does so by dropping most of the file's
rows. Confirms the row-count-loss check fires (row_loss_flagged=True) and is visible in
result.summary(), while the file is still (correctly) reported "cleaned" since the
flagged issue really is gone — the over-aggressiveness is a flag, not a failure.

Run with:
    printf 'yes\\nyes\\nyes\\nyes\\nyes\\nyes\\n' | uv run python -m tests.test_data_cleaning_post_validation
"""

import pandas as pd

from utils.data_cleaning import check_rubric, clean_dataset


class FakeResponse:
    def __init__(self, content):
        self.content = content


print("=" * 70)
print("CASE 1: a genuinely fixable single fail-level issue — real LLM, expect a clean pass")
print("=" * 70)

D1 = "data/_test_etl/postcheck_success"
issues1 = check_rubric(f"{D1}/orders.csv")
print("issues:", issues1)
assert len(issues1) == 1 and "negative" in issues1[0]

result1 = clean_dataset(D1, llm=None)  # real pick_llm("high")
print(result1.summary())
assert len(result1.cleaned_files) == 1, f"expected 1 cleaned file, got: {result1.cleaned_files}"
rec1 = result1.cleaned_files[0]
assert rec1.rubric_recheck_passed is True, "expected the post-cleaning re-check to pass"
assert rec1.remaining_issues == [], f"expected no remaining issues, got: {rec1.remaining_issues}"
assert "Final overall check: PASSED" in result1.summary()
cleaned1 = pd.read_csv(f"{D1}/cleaned/orders.csv")
assert (cleaned1["amount"].dropna() < 0).sum() == 0, "negative value should have been fixed"
print("PASS: genuinely fixed issue -> final re-check passes, reflected in the record and summary.\n")


print("=" * 70)
print("CASE 2: a deliberately incomplete warn-level fix — fake LLM's first batch attempt")
print("does not actually fix the batch's issue; second attempt does")
print("=" * 70)

D2 = "data/_test_etl/postcheck_incomplete"
CLONE2 = f"{D2}/cleaned/orders.csv"
issues2 = check_rubric(f"{D2}/orders.csv")
print("issues:", issues2)
assert len(issues2) == 2, f"expected exactly 2 issues in the fixture, got: {issues2}"
fail_issue = next(i for i in issues2 if i.startswith("Invalid values:"))
warn_issue = next(i for i in issues2 if i.startswith("Inconsistent categorical"))


class FixFailImmediatelyThenFixWarnOnRetryLLM:
    """Called once per group's own attempt loop. The fail-level issue's calls always
    return a real, successful fix. The warn-level batch's FIRST call returns a real,
    executable script that does NOT touch 'status' at all (leaving the categorical
    inconsistency completely untouched, so the post-execution re-check must still find
    it); the batch's SECOND call actually fixes it."""

    def __init__(self):
        self.calls = []
        self.warn_calls = 0

    def invoke(self, messages):
        human_content = messages[1][1]
        self.calls.append(human_content)
        if fail_issue in human_content and warn_issue not in human_content:
            return FakeResponse(
                "import pandas as pd\n"
                f"path = {CLONE2!r}\n"
                "df = pd.read_csv(path)\n"
                "df.loc[df['amount'] < 0, 'amount'] = df.loc[df['amount'] < 0, 'amount'].abs()\n"
                "df.to_csv(path, index=False)\n"
            )
        # Warn-level batch call.
        self.warn_calls += 1
        if self.warn_calls == 1:
            return FakeResponse(
                "import pandas as pd\n"
                f"path = {CLONE2!r}\n"
                "df = pd.read_csv(path)\n"
                "df.to_csv(path, index=False)\n"  # no-op: leaves 'status' untouched
            )
        return FakeResponse(
            "import pandas as pd\n"
            f"path = {CLONE2!r}\n"
            "df = pd.read_csv(path)\n"
            "df['status'] = df['status'].str.strip().str.lower()\n"
            "df.to_csv(path, index=False)\n"
        )


fake_llm_2 = FixFailImmediatelyThenFixWarnOnRetryLLM()
result2 = clean_dataset(D2, llm=fake_llm_2)
print(result2.summary())

assert len(result2.cleaned_files) == 1, f"expected 1 cleaned file, got: {result2.cleaned_files + result2.skipped_files}"
rec2 = result2.cleaned_files[0]
assert len(rec2.fail_issue_records) == 1 and rec2.fail_issue_records[0].status == "resolved"
assert len(rec2.warn_batches) == 1
assert rec2.warn_batches[0].status == "resolved", (
    f"expected the warn batch eventually resolved, got: {rec2.warn_batches[0]}"
)
assert rec2.warn_batches[0].attempts == 2, (
    f"expected the warn batch to need 2 attempts (first was a no-op), got {rec2.warn_batches[0].attempts}"
)
assert rec2.rubric_recheck_passed is True
print("PASS: post-cleaning re-check caught the still-present warn-level issue after a "
      "no-op first attempt and triggered a real retry; file only reported cleaned once "
      "both the fail-level issue and the warn-level batch were actually resolved.\n")


print("=" * 70)
print("CASE 3: a deliberately over-aggressive fix — drops most rows to 'fix' the issue")
print("=" * 70)

D3 = "data/_test_etl/postcheck_rowloss"
CLONE3 = f"{D3}/cleaned/orders.csv"
issues3 = check_rubric(f"{D3}/orders.csv")
print("issues:", issues3)
assert len(issues3) == 1 and "negative" in issues3[0]


class DropMostRowsLLM:
    """'Fixes' the negative-amount issue by deleting every row except the first two —
    the flagged issue really is gone (there's no negative value left at all), but this
    is a real, executable over-aggressive fix that should trip the row-loss flag."""

    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        self.calls.append(messages[1][1])
        return FakeResponse(
            "import pandas as pd\n"
            f"path = {CLONE3!r}\n"
            "df = pd.read_csv(path)\n"
            "df = df.head(2)\n"
            "df.to_csv(path, index=False)\n"
        )


fake_llm_3 = DropMostRowsLLM()
result3 = clean_dataset(D3, llm=fake_llm_3)
print(result3.summary())

assert len(fake_llm_3.calls) == 1, f"expected exactly 1 generation call, got {len(fake_llm_3.calls)}"
assert len(result3.cleaned_files) == 1, f"expected 1 cleaned file, got: {result3.cleaned_files}"
rec3 = result3.cleaned_files[0]
assert rec3.rubric_recheck_passed is True, "the flagged issue really is gone, so this should pass"
assert rec3.row_count_before == 10, f"expected original row count 10, got {rec3.row_count_before}"
assert rec3.row_count_after == 2, f"expected cleaned row count 2, got {rec3.row_count_after}"
assert rec3.row_loss_flagged is True, "expected the row-count-loss check to fire for an 80% loss"
assert "WARNING: lost" in result3.summary(), "expected the row-loss warning visible in summary()"
print("PASS: over-aggressive fix (80% row loss) correctly flagged, visible in the real summary.\n")

print("=" * 70)
print("ALL POST-CLEANING VALIDATION ASSERTIONS PASSED")
print("=" * 70)

