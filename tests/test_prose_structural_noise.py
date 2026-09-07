"""
Spec 1, Part 3: Job Description structural noise (bullet characters, embedded
newlines) inside long-form prose columns.

The reference notebook for this dataset replaces bullet characters with
periods and collapses embedded newlines into spaces in "Job Description".
No existing check targeted this, and discovery skips long-form prose columns
entirely (_is_long_form_prose_column) — so this pattern was structurally
invisible to both the static rubric and discovery. _check_prose_structural_noise
is a new, deterministic, no-LLM warn-level check (same tier as
_check_formatting_noise) that closes that gap.

Test 1: a synthetic long-form prose column with bullets and embedded newlines
is flagged; a short, ordinary text column with the same characters is not
(mirrors _is_long_form_prose_column's own mean-length gate).
Test 2: a clean prose column (no bullets, no newlines) produces no issue.
Test 3: the issue is warn-level and lands in check_rubric()'s full output.
Test 4: live check against the real Uncleaned_DS_jobs.csv "Job Description"
column.
Test 5: end-to-end through clean_dataset() — the issue is fixed via the
existing standard approval-gated flow (no menu, no judgment-call choice).
"""

import shutil
import sys
import tempfile
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pandas as pd

import utils.data_cleaning as dc


@contextmanager
def redirect_stdin_yes():
    original_stdin = sys.stdin
    sys.stdin = StringIO("yes\n" * 5)
    try:
        yield
    finally:
        sys.stdin = original_stdin


LONG_PREFIX = "Background. " * 20  # pads mean length past the 200-char prose threshold


print("=" * 70)
print("TEST 1: prose column with bullets/newlines flagged; short text column")
print("with the same characters is not (prose-length gate)")
print("=" * 70)

df1 = pd.DataFrame({
    "job_description": [
        LONG_PREFIX + "• Build models\n• Ship code",
        LONG_PREFIX + "Responsibilities include:\nWriting SQL and Python.",
        LONG_PREFIX + "Plain description with no noise at all here today.",
    ],
    "short_note": ["• ok", "• fine\nreally", "• great"],
})
issues1 = dc._check_prose_structural_noise(df1)
by_col1 = {i.split("'")[1]: i for i in issues1}
assert "job_description" in by_col1, f"expected job_description flagged, got {issues1}"
assert "short_note" not in by_col1, (
    f"a short (non-prose) column must not be flagged even with the same bullet/newline "
    f"characters, got {issues1}"
)
assert "1 value(s) contain bullet characters" in by_col1["job_description"], by_col1["job_description"]
assert "2 value(s) contain embedded newlines" in by_col1["job_description"], by_col1["job_description"]
print("PASS: prose column flagged; short text column correctly excluded by the prose-length gate.\n")

print("=" * 70)
print("TEST 2: a clean prose column (no bullets, no newlines) is not flagged")
print("=" * 70)

df2 = pd.DataFrame({
    "job_description": [
        LONG_PREFIX + "This role focuses on building robust data pipelines for the team.",
        LONG_PREFIX + "A senior analyst will own reporting across several business units.",
    ],
})
issues2 = dc._check_prose_structural_noise(df2)
assert issues2 == [], f"expected no issues for clean prose, got {issues2}"
print("PASS: clean prose column produces no issue.\n")

print("=" * 70)
print("TEST 3: warn-level, and present in check_rubric()'s full output")
print("=" * 70)

assert dc._issue_severity(by_col1["job_description"]) == "warn", (
    "structural prose noise must be warn-level, not fail-level — there's a clearly "
    "correct mechanical fix but it's not a fail-level correctness gate"
)
tmp_dir3 = Path(tempfile.mkdtemp(prefix="prose_noise_"))
try:
    file_path3 = tmp_dir3 / "jobs.csv"
    df1.to_csv(file_path3, index=False)
    full_issues3 = dc.check_rubric(file_path3)
    assert any(i.startswith("Structural noise in prose:") for i in full_issues3), (
        f"expected the prose-noise check wired into check_rubric(), got {full_issues3}"
    )
    print("PASS: issue is warn-level and appears in check_rubric()'s combined output.\n")
finally:
    shutil.rmtree(tmp_dir3, ignore_errors=True)

print("=" * 70)
print("TEST 4: live check against the real Uncleaned_DS_jobs.csv")
print("=" * 70)

real_path = "data/data-science-jobs/Uncleaned_DS_jobs.csv"
real_df = dc._read_csv_robust(real_path)
real_issues = dc._check_prose_structural_noise(real_df)
jd_issue = next((i for i in real_issues if "'Job Description'" in i), None)
assert jd_issue is not None, f"expected 'Job Description' flagged, got {real_issues}"
print(f"PASS: {jd_issue}\n")

print("=" * 70)
print("TEST 5: end-to-end through clean_dataset() — standard approval-gated fix,")
print("no menu, no judgment-call choice")
print("=" * 70)


class ProseFixLLM:
    """Idempotent fix covering BOTH the pre-existing 'Column misalignment'
    check (an embedded real newline inside a quoted CSV field always trips
    that check's unmatched-quote-per-physical-line heuristic) and this spec's
    new prose-noise check — collapsing embedded newlines/bullets removes the
    multi-line quoted field entirely, resolving both regardless of which
    issue triggered this call, and is safe to run more than once."""

    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        human_text = messages[-1][1]
        import re
        match = re.search(r"File to clean \(read and overwrite this exact path\): (\S+)", human_text)
        path = match.group(1) if match else str(file_path5)
        code = (
            "import pandas as pd\n"
            f"df = pd.read_csv(r'{path}', dtype=str, keep_default_na=True)\n"
            "for ch in ['\\u2022', '\\u25e6', '\\u25aa', '\\u2023', '\\u25cf', '\\u2219']:\n"
            "    df['job_description'] = df['job_description'].str.replace(ch, '.', regex=False)\n"
            "df['job_description'] = df['job_description'].str.replace('\\n', ' ', regex=False)\n"
            f"df.to_csv(r'{path}', index=False)\n"
        )

        class _Resp:
            content = code

        return _Resp()


df5 = pd.DataFrame({
    "job_description": [
        LONG_PREFIX + "\u2022 Build models\n\u2022 Ship code",
        LONG_PREFIX + "Responsibilities include:\nWriting SQL and Python.",
        LONG_PREFIX + "Plain description with no noise at all here today.",
    ],
})

tmp_dir5 = Path(tempfile.mkdtemp(prefix="prose_noise_fix_"))
try:
    file_path5 = tmp_dir5 / "jobs.csv"
    df5.to_csv(file_path5, index=False)
    fake_llm5 = ProseFixLLM()
    with redirect_stdin_yes():
        result5 = dc.clean_dataset(str(tmp_dir5), llm=fake_llm5)
    assert len(result5.cleaned_files) == 1, f"expected 1 cleaned file, got {result5.summary()}"
    rec5 = result5.cleaned_files[0]
    # "Column misalignment" (fail-level, pre-existing check) always co-occurs with a real
    # embedded newline \u2014 a quoted multi-line CSV field looks like an unmatched quote on
    # any single physical line. This spec's fix (collapsing the newline) resolves both.
    prose_records = [
        r for r in rec5.fail_issue_records
        if r.issue.startswith("Structural noise in prose:")
    ]
    # This issue is warn-level, so it's routed via warn_batches, not fail_issue_records.
    prose_records = prose_records or [
        b for b in rec5.warn_batches
        if any(i.startswith("Structural noise in prose:") for i in b.issues)
    ]
    assert prose_records, f"expected the prose-noise issue processed via the standard flow, got {rec5}"
    assert all(
        r.status == "resolved"
        for r in rec5.fail_issue_records
        if r.issue.startswith("Column misalignment:")
    ), f"expected the pre-existing misalignment issue also resolved by the same fix, got {rec5.fail_issue_records}"
    fixed_df = pd.read_csv(tmp_dir5 / "cleaned" / "jobs.csv", dtype=str)
    assert not fixed_df["job_description"].str.contains("\n").any(), "newlines should be gone"
    assert not fixed_df["job_description"].str.contains("\u2022").any(), "bullets should be gone"
    print("PASS: prose structural noise fixed through the standard approval-gated flow.\n")
finally:
    shutil.rmtree(tmp_dir5, ignore_errors=True)

print("=" * 70)
print("ALL PROSE-STRUCTURAL-NOISE (SPEC 1, PART 3) ASSERTIONS PASSED")
print("=" * 70)
