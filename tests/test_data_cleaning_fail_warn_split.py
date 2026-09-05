"""Standalone test for the fail-level/warn-level restructuring of clean_dataset():
fail-level issues are processed ONE AT A TIME (their own generate/approve/execute/
immediate re-check cycle each), never batched together; warn-level issues stay
batched into one combined cycle, unchanged from the original whole-file design.

Case 1 (real LLM): a file with two distinct fail-level issues. Confirms each gets its
own separate approval prompt (2 GENERATED CLEANING CODE printouts, one per issue) and
its own immediate re-check — not one combined pass covering both at once.

Case 2 (fake LLM, deterministic): a file with two distinct fail-level issues where the
first fix succeeds and the second deliberately keeps failing (a real execution error
every attempt). Confirms the first issue is correctly resolved and reported as such,
the second is correctly skipped after exhausting MAX_CLEAN_ATTEMPTS, and — critically —
processing continues to completion (the final full check) rather than aborting the rest
of the file over the one unresolved issue.

Case 3 (real LLM): a file with ONLY warn-level issues. Confirms they're still batched
together in one single generate/approve/execute/re-check pass, unchanged from before
this restructuring (one GENERATED CLEANING CODE printout covering both issues at once).

Run with:
    printf 'yes\\nyes\\nyes\\nyes\\nyes\\nyes\\nyes\\nyes\\n' | uv run python -m tests.test_data_cleaning_fail_warn_split
"""

import re

from utils.data_cleaning import MAX_CLEAN_ATTEMPTS, check_rubric, clean_dataset


class FakeResponse:
    def __init__(self, content):
        self.content = content


def count_approval_prompts(captured_stdout: str) -> int:
    return len(re.findall(r"GENERATED CLEANING CODE for:", captured_stdout))


print("=" * 70)
print("CASE 1: two distinct fail-level issues, real LLM — separate approvals expected")
print("=" * 70)

D1 = "data/_test_etl/two_fail_issues"
issues1 = check_rubric(f"{D1}/orders.csv")
print("issues:", issues1)
assert len(issues1) == 2, f"expected exactly 2 fail-level issues in the fixture, got: {issues1}"

import io
import contextlib

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    result1 = clean_dataset(D1, llm=None)  # real pick_llm("high")
captured1 = buf.getvalue()
print(captured1)
print(result1.summary())

prompt_count1 = count_approval_prompts(captured1)
assert prompt_count1 == 2, (
    f"expected exactly 2 separate approval prompts (one per fail-level issue), "
    f"got {prompt_count1}"
)
print(f"PASS: {prompt_count1} separate approval prompts confirmed — one per fail-level issue.")

assert len(result1.cleaned_files) == 1, f"expected 1 cleaned file, got: {result1.cleaned_files + result1.skipped_files}"
rec1 = result1.cleaned_files[0]
assert len(rec1.fail_issue_records) == 2, (
    f"expected 2 individually-processed fail-level issue records, got: {rec1.fail_issue_records}"
)
for issue_rec in rec1.fail_issue_records:
    assert issue_rec.status == "resolved", f"expected each fail-level issue resolved, got: {issue_rec}"
print("PASS: both fail-level issues individually resolved and recorded separately.\n")


print("=" * 70)
print("CASE 2: first fail-level issue resolves, second deliberately keeps failing")
print("=" * 70)

D2 = "data/_test_etl/fail_then_stuck"
CLONE2 = f"{D2}/cleaned/orders.csv"
issues2 = check_rubric(f"{D2}/orders.csv")
print("issues:", issues2)
assert len(issues2) == 2, f"expected exactly 2 fail-level issues in the fixture, got: {issues2}"
dup_issue = next(i for i in issues2 if i.startswith("Duplicate values:"))
negative_issue = next(i for i in issues2 if i.startswith("Invalid values:"))


class FirstResolvesSecondNeverDoesLLM:
    """Called once per fail-level issue's own attempt loop (a fresh call each time
    clean_dataset invokes _clean_issue_group for a new issue, or retries the same one).
    Distinguishes which issue is being targeted by inspecting the human message content
    (each call is scoped to exactly one issue's text), and always returns code that
    raises for the negative-amount issue, or a real working fix for the duplicate-id
    issue."""

    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        human_content = messages[1][1]
        self.calls.append(human_content)
        if dup_issue in human_content:
            return FakeResponse(
                "import pandas as pd\n"
                f"path = {CLONE2!r}\n"
                "df = pd.read_csv(path)\n"
                "df['order_id'] = range(1, len(df) + 1)\n"
                "df.to_csv(path, index=False)\n"
            )
        # negative_issue's own generation calls: always broken, deliberately.
        return FakeResponse("raise RuntimeError('deliberately never fixed, attempt logged')")


fake_llm_2 = FirstResolvesSecondNeverDoesLLM()
result2 = clean_dataset(D2, llm=fake_llm_2)
print(result2.summary())

assert len(result2.skipped_files) == 1, f"expected 1 skipped file, got: {result2.cleaned_files + result2.skipped_files}"
rec2 = result2.skipped_files[0]
assert rec2.status == "skipped_incomplete", f"expected status skipped_incomplete, got {rec2.status}"
assert len(rec2.fail_issue_records) == 2, (
    f"expected both fail-level issues to have their own record (not aborted after the "
    f"first failure), got: {rec2.fail_issue_records}"
)

dup_rec = next(r for r in rec2.fail_issue_records if r.issue == dup_issue)
neg_rec = next(r for r in rec2.fail_issue_records if r.issue == negative_issue)
assert dup_rec.status == "resolved", f"expected the duplicate-id issue resolved, got: {dup_rec}"
assert neg_rec.status == "skipped_failed", f"expected the negative-amount issue skipped_failed, got: {neg_rec}"
assert neg_rec.attempts == MAX_CLEAN_ATTEMPTS, (
    f"expected the stuck issue to exhaust all {MAX_CLEAN_ATTEMPTS} attempts, got {neg_rec.attempts}"
)
print("PASS: first issue correctly resolved, second correctly skipped after exhausting attempts.")

# Critically: processing did NOT abort after the second issue's failure -- it reached
# the warn-level stage (there are none in this fixture, so it's reported as such) and
# the final overall check, rather than the file record being cut short.
assert len(rec2.warn_batches) == 1, (
    "expected processing to continue to the warn-level stage rather than aborting "
    "over the one unresolved fail-level issue"
)
assert rec2.warn_batches[0].status == "no_warn_issues"
assert rec2.remaining_issues == [negative_issue], (
    f"expected only the still-broken issue in the final remaining_issues, got: {rec2.remaining_issues}"
)
print("PASS: processing continued past the unresolved issue to the warn-level stage and "
      "final check, rather than aborting the rest of the file.\n")


print("=" * 70)
print("CASE 3: a file with ONLY warn-level issues — still batched together, unchanged")
print("=" * 70)

D3 = "data/_test_etl/warn_only"
issues3 = check_rubric(f"{D3}/records.csv")
print("issues:", issues3)
assert len(issues3) == 2, f"expected exactly 2 warn-level issues in the fixture, got: {issues3}"

buf3 = io.StringIO()
with contextlib.redirect_stdout(buf3):
    result3 = clean_dataset(D3, llm=None)  # real pick_llm("high")
captured3 = buf3.getvalue()
print(captured3)
print(result3.summary())

prompt_count3 = count_approval_prompts(captured3)
assert prompt_count3 == 1, (
    f"expected exactly 1 approval prompt (both warn-level issues batched together), "
    f"got {prompt_count3}"
)
print(f"PASS: {prompt_count3} approval prompt confirmed — both warn-level issues batched together.")

assert len(result3.cleaned_files) == 1, f"expected 1 cleaned file, got: {result3.cleaned_files + result3.skipped_files}"
rec3 = result3.cleaned_files[0]
assert rec3.fail_issue_records == [], f"expected no fail-level issue records, got: {rec3.fail_issue_records}"
assert len(rec3.warn_batches) == 1 and rec3.warn_batches[0].status == "resolved"
assert len(rec3.warn_batches[0].issues) == 2
print("PASS: file with only warn-level issues processed as one single batched pass.\n")

print("=" * 70)
print("ALL FAIL/WARN SEVERITY-SPLIT RESTRUCTURING ASSERTIONS PASSED")
print("=" * 70)
