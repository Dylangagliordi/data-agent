"""
Tests for Spec 3: numeric range decomposition (Salary/Revenue -> structured
min/max/avg columns).

This is deliberately NOT the same kind of fix as composite-field splitting. A
composite field is a data-quality PROBLEM (two variables wrongly glued together);
a salary/revenue range like "$137K-$171K" is not wrong — decomposing it into
min_salary/max_salary/avg_salary is a deliberate schema-ENRICHMENT decision. It
gets its own detection (_detect_range_columns, never added to check_rubric's
issue list), its own prompt (RANGE_DECOMPOSITION_SYSTEM_PROMPT), and its own
opt-in fix path (decompose_range_column) that clean_dataset() never calls
automatically.

Test 1: detection against a cleaned Uncleaned_DS_jobs.csv-style fixture (real
"$137K-$171K (Glassdoor est.)" / "$1 to $2 billion (USD)" shapes) — confirms
salary_estimate and revenue are both correctly identified as candidates with a
real match fraction >= RANGE_DECOMPOSITION_MATCH_THRESHOLD, and that nothing
from this detection ever appears in check_rubric's issue list.

Test 2: opt-in decomposition, real split — decompose_range_column called
directly on the salary candidate; confirms the approval gate is invoked, the
original column survives byte-for-byte unchanged, and min_salary/max_salary/
avg_salary appear with correct values for matching rows and null for a
deliberately non-matching row.

Test 3: decline path, no retry — a fake LLM returns the NO_DECOMPOSITION
sentinel for a candidate that isn't actually a clean range; confirms exactly
one LLM call (no retry), no code execution, the file untouched, and the
outcome "declined_false_positive" with the reason preserved.

Test 4: not auto-triggered — running clean_dataset() normally on a fixture with
an obvious range column populates structured_decomposition_candidates on the
FileCleaningRecord but never itself calls decompose_range_column or changes
the file's column count.
"""

import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pandas as pd

import utils.data_cleaning as dc


@contextmanager
def redirect_stdin_yes(count=10):
    """Same trick as test_composite_field_split.py / test_composite_field_decline.py:
    these tests run as one in-process script, so monkeypatch sys.stdin directly
    rather than relying on a shell-level pipe."""
    original_stdin = sys.stdin
    sys.stdin = StringIO("yes\n" * count)
    try:
        yield
    finally:
        sys.stdin = original_stdin


class _FakeResponse:
    def __init__(self, content):
        self.content = content


N = 25
# Real shapes from data/data-science-jobs/Uncleaned_DS_jobs.csv, reproduced here
# as a "cleaned" fixture (i.e. as they'd look after check_rubric + discovery-
# phase processing has already run on the raw file, which this spec's detection
# is wired to run AFTER — see clean_dataset()'s wiring).
salary_values = ["$137K-$171K (Glassdoor est.)"] * (N - 1) + ["Unknown"]
revenue_values = ["$1 to $2 billion (USD)"] * (N - 1) + ["-1"]
job_titles = [f"Data Scientist {i}" for i in range(N)]


print("=" * 70)
print("TEST 1: detection against a cleaned Uncleaned_DS_jobs.csv-style fixture")
print("=" * 70)

df1 = pd.DataFrame({
    "Salary Estimate": salary_values,
    "Revenue": revenue_values,
    "Job Title": job_titles,
})
candidates1 = dc._detect_range_columns(df1)
print("candidates:", candidates1)

by_column = {c["column"]: c for c in candidates1}
assert "Salary Estimate" in by_column, f"expected 'Salary Estimate' detected, got {candidates1}"
assert by_column["Salary Estimate"]["match_fraction"] >= dc.RANGE_DECOMPOSITION_MATCH_THRESHOLD
assert "Revenue" in by_column, f"expected 'Revenue' detected, got {candidates1}"
assert by_column["Revenue"]["match_fraction"] >= dc.RANGE_DECOMPOSITION_MATCH_THRESHOLD
assert "Job Title" not in by_column, "a plain text column must never be a range candidate"
print("PASS: both 'Salary Estimate' and 'Revenue' correctly identified as range candidates.\n")

# Confirm range detection is entirely separate from check_rubric's vocabulary.
# check_rubric legitimately still flags OTHER, unrelated pre-existing issues on
# this raw-ish text (e.g. "Currency/unit symbols" on the literal "$137K", or
# "Placeholder values" on the one "Unknown" row) -- that's correct, expected
# behavior of checks that predate this spec, not something range detection
# should suppress. What must be true is that NOTHING resembling a "range
# detected" / "structured decomposition" issue category exists anywhere in
# check_rubric's vocabulary or this file's actual issue list.
tmp_check = Path(tempfile.mkdtemp(prefix="range_detect_"))
try:
    csv_path = tmp_check / "jobs.csv"
    df1.to_csv(csv_path, index=False)
    rubric_issues = dc.check_rubric(csv_path)
    print("check_rubric issues:", rubric_issues)
    assert not any("range" in i.lower() or "decompos" in i.lower() for i in rubric_issues), (
        f"range detection must never surface AS a check_rubric issue category, got {rubric_issues}"
    )
    for prefix in dc.FAIL_LEVEL_PREFIXES + dc.WARN_LEVEL_PREFIXES:
        assert "range" not in prefix.lower() and "decompos" not in prefix.lower(), (
            f"no FAIL/WARN prefix should exist for range decomposition, found {prefix!r}"
        )
    print("PASS: check_rubric's vocabulary and this file's issue list contain nothing "
          "resembling a range-decomposition category (its own, unrelated legitimate "
          "checks on these columns are untouched by this spec).\n")
finally:
    shutil.rmtree(tmp_check, ignore_errors=True)


print("=" * 70)
print("TEST 2: opt-in decomposition — real split via decompose_range_column")
print("=" * 70)


class SalarySplitLLM:
    """A real, working decomposition script for the Salary Estimate candidate.
    Extracts the two $NNNK bounds, converts to plain thousands-of-dollars
    integers, and computes an average — leaving null for a non-matching value."""

    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        human_text = messages[-1][1]
        assert "Salary Estimate" in human_text
        code = (
            "import pandas as pd\n"
            "import re\n"
            "path = 'PLACEHOLDER'\n"
            "df = pd.read_csv(path, dtype=str)\n"
            "def parse(v):\n"
            "    if not isinstance(v, str):\n"
            "        return (None, None)\n"
            "    m = re.match(r'^\\$(\\d+)K-\\$(\\d+)K', v)\n"
            "    if not m:\n"
            "        return (None, None)\n"
            "    return (int(m.group(1)), int(m.group(2)))\n"
            "parsed = df['Salary Estimate'].map(parse)\n"
            "df['min_salary'] = parsed.map(lambda t: t[0])\n"
            "df['max_salary'] = parsed.map(lambda t: t[1])\n"
            "df['avg_salary'] = df[['min_salary', 'max_salary']].mean(axis=1)\n"
            "df.to_csv(path, index=False)\n"
        )
        # Substitute the real path in from the prompt (mirrors how the other
        # fake LLMs in this project's test suite extract the real file path).
        import re as _re
        m = _re.search(r"exact path\): (.+)", human_text)
        real_path = m.group(1).strip()
        return _FakeResponse(code.replace("PLACEHOLDER", real_path))


tmp_dir2 = Path(tempfile.mkdtemp(prefix="range_decompose_"))
try:
    file_path2 = tmp_dir2 / "jobs.csv"
    df2 = pd.DataFrame({"Salary Estimate": salary_values, "Job Title": job_titles})
    df2.to_csv(file_path2, index=False)

    candidate2 = {
        "column": "Salary Estimate", "match_fraction": 0.96,
        "sample_pattern": "$137K-$171K (Glassdoor est.)",
    }
    fake_llm2 = SalarySplitLLM()

    stdout_capture = StringIO()
    original_stdout = sys.stdout
    sys.stdout = stdout_capture
    try:
        with redirect_stdin_yes(count=dc.MAX_CLEAN_ATTEMPTS + 2):
            result2 = dc.decompose_range_column(file_path2, candidate2, llm=fake_llm2)
    finally:
        sys.stdout = original_stdout
    printed = stdout_capture.getvalue()
    print(printed[-1500:])
    print("result2:", result2)

    assert "GENERATED CLEANING CODE for:" in printed, (
        "the real approval gate (_request_approval) must have been invoked"
    )
    assert result2["status"] == "resolved", f"expected 'resolved', got {result2}"
    assert set(result2["new_columns"]) == {"min_salary", "max_salary", "avg_salary"}, (
        f"expected the 3 new columns reported, got {result2['new_columns']}"
    )

    result_df2 = pd.read_csv(file_path2)
    print("columns:", list(result_df2.columns))
    assert "Salary Estimate" in result_df2.columns, "the original column must be preserved"
    assert result_df2["Salary Estimate"].tolist() == salary_values, (
        "the original column must be byte-for-byte unchanged"
    )
    for i in range(N - 1):
        assert result_df2.loc[i, "min_salary"] == 137, result_df2.loc[i]
        assert result_df2.loc[i, "max_salary"] == 171, result_df2.loc[i]
        assert result_df2.loc[i, "avg_salary"] == 154.0, result_df2.loc[i]
    assert pd.isna(result_df2.loc[N - 1, "min_salary"]) and pd.isna(result_df2.loc[N - 1, "max_salary"]), (
        "the non-matching 'Unknown' row must be null in the new columns, not fabricated"
    )
    assert result_df2["Job Title"].tolist() == job_titles, "Job Title must be untouched"
    print("PASS: real split resolved — original column unchanged, correct values for "
          "matching rows, null for the non-matching row, other columns untouched.\n")
finally:
    shutil.rmtree(tmp_dir2, ignore_errors=True)


print("=" * 70)
print("TEST 3: NO_DECOMPOSITION decline — exactly one LLM call, no execution,")
print("file completely unchanged, reason preserved")
print("=" * 70)


class AlwaysDeclineDecompositionLLM:
    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        return _FakeResponse(
            "# NO_DECOMPOSITION: this column is mostly non-numeric placeholders, not a real range."
        )


tmp_dir3 = Path(tempfile.mkdtemp(prefix="range_decline_"))
try:
    file_path3 = tmp_dir3 / "jobs.csv"
    df3 = pd.DataFrame({"Revenue": revenue_values, "Job Title": job_titles})
    df3.to_csv(file_path3, index=False)
    columns_before = list(pd.read_csv(file_path3, dtype=str).columns)

    candidate3 = {"column": "Revenue", "match_fraction": 0.96, "sample_pattern": "$1 to $2 billion (USD)"}
    fake_llm3 = AlwaysDeclineDecompositionLLM()

    # Deliberately NOT wrapped in redirect_stdin_yes: if this ever reaches
    # _request_approval, input() will raise EOFError against real stdin.
    result3 = dc.decompose_range_column(file_path3, candidate3, llm=fake_llm3)
    print("result3:", result3)

    assert fake_llm3.call_count == 1, f"expected exactly 1 LLM call (no retry), got {fake_llm3.call_count}"
    assert result3["status"] == "declined_false_positive", f"expected the decline, got {result3}"
    assert result3["error"] == "this column is mostly non-numeric placeholders, not a real range.", (
        f"the model's stated reason must be preserved verbatim, got {result3['error']!r}"
    )
    assert result3["new_columns"] == []
    assert result3["generated_code"] == ""

    columns_after = list(pd.read_csv(file_path3, dtype=str).columns)
    assert columns_after == columns_before, (
        f"the file must be completely untouched: {columns_before} -> {columns_after}"
    )
    print("PASS: exactly 1 LLM call, no execution, file unchanged, decline reason preserved.\n")
finally:
    shutil.rmtree(tmp_dir3, ignore_errors=True)


print("=" * 70)
print("TEST 4: not auto-triggered — clean_dataset() populates candidates but")
print("never itself calls decompose_range_column or changes column count")
print("=" * 70)


class RangeSentinelLLM:
    """Confirms clean_dataset() never invokes the range-decomposition generator
    (RANGE_DECOMPOSITION_SYSTEM_PROMPT) or the composite-field split generator on
    its own — that is the actual thing under test here. A "$"-prefixed range
    column like this fixture's Salary Estimate legitimately still trips
    check_rubric's OWN, pre-existing "Currency/unit symbols" warn check (that
    check predates this spec and is correctly unrelated to it), so ordinary
    cleaning-code-generation calls ARE expected and are handled with a harmless
    no-op script; discovery's own with_structured_output(...) calls aren't
    implemented on this fake at all and are silently caught as "nothing
    noticed" by explore_column/_verify_hypothesis, same as any other plain-
    .invoke()-only fake elsewhere in this test suite."""

    def invoke(self, messages):
        system_text = messages[0][1]
        assert system_text != dc.RANGE_DECOMPOSITION_SYSTEM_PROMPT, (
            "clean_dataset() must NEVER call the range-decomposition generator itself"
        )
        assert system_text != dc.COMPOSITE_FIELD_SPLIT_SYSTEM_PROMPT, (
            "this fixture has no composite-field issue — unexpected generator call"
        )
        human_text = messages[-1][1]
        match = re.search(r"exact path\): (.+)", human_text)
        path = match.group(1).strip()
        # Harmless no-op: read/write unchanged. Doesn't need to actually resolve
        # the warn-level currency-symbol issue — this test only cares that no
        # decomposition columns ever appear, regardless of the file's final
        # cleaned/skipped_incomplete status.
        return _FakeResponse(f"import pandas as pd\npd.read_csv({path!r}, dtype=str).to_csv({path!r}, index=False)\n")


tmp_dir4 = Path(tempfile.mkdtemp(prefix="range_not_auto_"))
try:
    raw_path4 = tmp_dir4 / "jobs.csv"
    df4 = pd.DataFrame({
        "Salary Estimate": ["$100K-$150K (Glassdoor est.)"] * N,
        "Job Title": [f"Engineer {i}" for i in range(N)],
    })
    df4.to_csv(raw_path4, index=False)

    with redirect_stdin_yes(count=dc.MAX_CLEAN_ATTEMPTS + 2):
        result4 = dc.clean_dataset(str(tmp_dir4), llm=RangeSentinelLLM())
    print(result4.summary())

    all_records = result4.cleaned_files + result4.skipped_files
    by_name = {r.file_name: r for r in all_records}

    if "jobs.csv" in by_name:
        rec = by_name["jobs.csv"]
        print("structured_decomposition_candidates:", rec.structured_decomposition_candidates)
        candidate_cols = {c["column"] for c in rec.structured_decomposition_candidates}
        assert "Salary Estimate" in candidate_cols, (
            f"expected 'Salary Estimate' listed as a candidate, got {rec.structured_decomposition_candidates}"
        )
        result_df4 = pd.read_csv(tmp_dir4 / "cleaned" / "jobs.csv", dtype=str)
    else:
        # Genuinely untouched (no cloning at all) — the file itself is proof
        # nothing was auto-decomposed, since there's no clean_dataset()-driven
        # write path for an untouched file at all.
        assert "jobs.csv" in result4.untouched_files, f"expected jobs.csv untouched or recorded, got {result4.summary()}"
        result_df4 = pd.read_csv(raw_path4, dtype=str)

    assert list(result_df4.columns) == ["Salary Estimate", "Job Title"], (
        f"clean_dataset() must NEVER add decomposition columns on its own, got {list(result_df4.columns)}"
    )
    print("PASS: clean_dataset() never auto-calls decompose_range_column and never "
          "changes the file's column count on its own.\n")
finally:
    shutil.rmtree(tmp_dir4, ignore_errors=True)


print("=" * 70)
print("ALL RANGE-DECOMPOSITION ASSERTIONS PASSED")
print("=" * 70)
