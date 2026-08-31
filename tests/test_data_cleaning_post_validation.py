"""Standalone test for Part 3: post-cleaning validation in clean_dataset().

Confirms clean_dataset() no longer treats "the generated code executed without raising"
as proof cleaning actually worked — it now re-runs check_rubric() against the real
cleaned output and reacts to what it finds there, plus tracks row-count loss.

Case 1 (real LLM): a straightforward, single genuinely-fixable issue. Confirms the
post-cleaning re-check passes and this is reflected in both the FileCleaningRecord and
result.summary() text.

Case 2 (fake LLM, deterministic): a file with TWO flagged issues. The fake LLM's first
response fixes only one of them (a real, executable script — no exception) and leaves
the other completely untouched. Confirms the post-cleaning re-check catches the still-
present issue and triggers a genuine retry (not a false "cleaned" report) — the second
call actually fixes the remaining issue, and only THEN is the file reported cleaned,
with attempts == 2.

Case 3 (fake LLM, deterministic): a file with one flagged issue, whose fake LLM response
technically fixes that one issue but does so by dropping most of the file's rows.
Confirms the row-count-loss check fires (row_loss_flagged=True) and is visible in
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
print("CASE 1: a genuinely fixable single issue — real LLM, expect a clean pass")
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
assert "Post-cleaning re-check: PASSED" in result1.summary()
cleaned1 = pd.read_csv(f"{D1}/cleaned/orders.csv")
assert (cleaned1["amount"].dropna() < 0).sum() == 0, "negative value should have been fixed"
print("PASS: genuinely fixed issue -> re-check passes, reflected in the record and summary.\n")


print("=" * 70)
print("CASE 2: a deliberately incomplete fix — fake LLM fixes only ONE of two issues")
print("=" * 70)

D2 = "data/_test_etl/postcheck_incomplete"
CLONE2 = f"{D2}/cleaned/orders.csv"
issues2 = check_rubric(f"{D2}/orders.csv")
print("issues:", issues2)
assert len(issues2) == 2, f"expected exactly 2 issues in the fixture, got: {issues2}"


class FixOneIssueThenTheOtherLLM:
    """Attempt 1: fixes only the negative-amount issue via a REAL, successful script —
    the categorical inconsistency in 'status' is left completely untouched, so the
    post-cleaning rubric re-check must still find it. Attempt 2: actually fixes it."""

    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        human_content = messages[1][1]
        self.calls.append(human_content)
        if len(self.calls) == 1:
            return FakeResponse(
                "import pandas as pd\n"
                f"path = {CLONE2!r}\n"
                "df = pd.read_csv(path)\n"
                "df.loc[df['amount'] < 0, 'amount'] = df.loc[df['amount'] < 0, 'amount'].abs()\n"
                "df.to_csv(path, index=False)\n"
            )
        return FakeResponse(
            "import pandas as pd\n"
            f"path = {CLONE2!r}\n"
            "df = pd.read_csv(path)\n"
            "df['status'] = df['status'].str.strip().str.lower()\n"
            "df.to_csv(path, index=False)\n"
        )


fake_llm_2 = FixOneIssueThenTheOtherLLM()
result2 = clean_dataset(D2, llm=fake_llm_2)
print(result2.summary())

assert len(fake_llm_2.calls) == 2, (
    f"expected exactly 2 generation calls (retry triggered by the still-present issue), "
    f"got {len(fake_llm_2.calls)}"
)
assert "STILL PRESENT" in fake_llm_2.calls[1], (
    "expected the second call's prompt to be told the specific issue was still present "
    "after the first (falsely 'successful') attempt"
)
assert "Inconsistent categorical" in fake_llm_2.calls[1]
print("PASS: post-cleaning re-check caught the still-present issue and fed it back for a real retry.")

assert len(result2.cleaned_files) == 1, f"expected 1 cleaned file, got: {result2.cleaned_files}"
rec2 = result2.cleaned_files[0]
assert rec2.attempts == 2, f"expected it to take 2 real attempts, got attempts={rec2.attempts}"
assert rec2.rubric_recheck_passed is True
print("PASS: file only reported 'cleaned' once BOTH issues were actually gone (attempts=2).\n")


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
print("ALL POST-CLEANING VALIDATION (PART 3) ASSERTIONS PASSED")
print("=" * 70)
