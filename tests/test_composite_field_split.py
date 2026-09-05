"""
Tests for the scoped fix path added for "Composite field (discovered):" issues in
utils/data_cleaning.py.

Every other issue category is fixed via the general-purpose _generate_cleaning_code /
CLEANING_CODE_SYSTEM_PROMPT, which explicitly forbids adding or dropping columns. A
composite-field split genuinely needs to produce two columns from one and drop the
original, so this one issue category is routed to a separate, explicitly-scoped
generator (_generate_composite_split_code / COMPOSITE_FIELD_SPLIT_SYSTEM_PROMPT)
instead — and because check_rubric() can never re-detect a "(discovered)" issue
string, an extra mechanical post-fix column-shape check (_composite_split_shape_ok)
does the REAL verification that the existing generic re-check cannot provide for
this issue type.

Test 1 (end-to-end via clean_dataset): a fixture CSV with a "Company Name" column
whose values are the company name and rating glued together with a newline (the real
Uncleaned_DS_jobs.csv shape) — one deliberately non-matching row included. A single
fake LLM plays all three roles clean_dataset() actually uses it for (exploration,
hypothesis verification, and fix-code generation), dispatched by call shape, so this
genuinely exercises the discovery -> fix-generation -> approval -> execution ->
shape-check pipeline exactly as clean_dataset() wires it, not a hand-assembled
shortcut. Confirms: the discovery pass flags the composite issue; the approval gate
is actually invoked (piped 'yes' stdin — this test would hang on a real
`input()` otherwise); the output has exactly two new columns with the original gone;
matching rows are correctly split; the deliberately non-matching row is null in both
new columns.

Test 2: every other column in the fixture is byte-for-byte unchanged.

Test 3 (test double): a fix that drops the flagged column with NO replacement columns
— confirms the post-fix shape check catches this and marks the issue
"skipped_incomplete" with the exact required reason string, even though check_rubric's
own re-check has nothing to say about it (it can never regenerate a "(discovered)"
string in the first place, so its "still present" check is trivially empty here).

Test 4: a missing pattern_lookup entry for a composite-field issue falls back to the
general-purpose _generate_cleaning_code instead of raising, with a logged warning.
"""

import re
import shutil
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr
from io import StringIO
from pathlib import Path

import pandas as pd

import utils.data_cleaning as dc
from models.schema import ExplorationHypothesis, VerifiedPatternProposal


@contextmanager
def redirect_stdin_yes(count=10):
    """These tests run as one in-process script (not one-shell-invocation-per-test
    like the rest of this project's piped-stdin approval tests), so the usual
    `printf 'yes\\n' | uv run python tests/test_x.py` trick can't target just one
    _clean_issue_group call within a larger script. This monkeypatches sys.stdin
    directly for the duration of the call: Python's builtin input() falls back to
    plain sys.stdin.readline() once sys.stdin is no longer the original tty-backed
    stream, so a StringIO of repeated 'yes\\n' answers every approval prompt inside
    the `with` block exactly like a piped stdin would."""
    original_stdin = sys.stdin
    sys.stdin = StringIO("yes\n" * count)
    try:
        yield
    finally:
        sys.stdin = original_stdin


class _FakeResponse:
    def __init__(self, content):
        self.content = content


COMPOSITE_HYPOTHESIS_TEXT = (
    "This column may hold two variables joined by a newline, with the second part "
    "looking like a decimal rating number."
)
COMPOSITE_REGEX = r"\n\d\.\d$"

_FILE_PATH_RE = re.compile(r"exact path\): (.+)")


class CompositeFixLLM:
    """Fake chat model playing all three roles clean_dataset() actually uses the SAME
    resolved llm for: (1) explore_column's with_structured_output(ExplorationHypothesis)
    — proposes the composite hypothesis for 'Company Name' only; (2) _verify_hypothesis's
    with_structured_output(VerifiedPatternProposal) — turns it into the real regex; (3) a
    PLAIN .invoke() (no with_structured_output preceding it) for fix-code generation —
    dispatches on the system prompt to return a real, working composite-split script
    when asked, and generic no-op cleaning code otherwise.

    with_structured_output(schema_cls) sets a one-shot pending schema that the very next
    invoke() consumes and clears — this is what lets the SAME object correctly tell a
    structured call apart from clean_dataset()'s later plain llm.invoke() code-gen calls,
    exactly mirroring how a real chat model is used in this codebase (schema requested
    fresh immediately before each structured call, never left set across calls).
    """

    def __init__(self):
        self._pending_schema = None
        self.code_gen_calls = 0

    def with_structured_output(self, schema_cls):
        self._pending_schema = schema_cls
        return self

    def invoke(self, messages):
        schema_cls = self._pending_schema
        self._pending_schema = None
        system_text = messages[0][1]
        human_text = messages[-1][1]

        if schema_cls is ExplorationHypothesis:
            if "Column name: Company Name" in human_text:
                return ExplorationHypothesis(hypotheses=[COMPOSITE_HYPOTHESIS_TEXT])
            return ExplorationHypothesis(hypotheses=[])

        if schema_cls is VerifiedPatternProposal:
            return VerifiedPatternProposal(
                pattern=COMPOSITE_REGEX, match_threshold=0.8,
                description="company name glued to a decimal rating via a newline",
            )

        # Plain invoke() — fix-code generation.
        self.code_gen_calls += 1
        match = _FILE_PATH_RE.search(human_text)
        assert match, f"expected the exact file path in the prompt, got: {human_text[:300]!r}"
        file_path = match.group(1).strip()

        if system_text == dc.COMPOSITE_FIELD_SPLIT_SYSTEM_PROMPT:
            code = (
                "import pandas as pd\n"
                f"path = {file_path!r}\n"
                "df = pd.read_csv(path, dtype=str)\n"
                f"split = df['Company Name'].str.extract(r'^(.*)\\n(\\d\\.\\d)$')\n"
                "df['company_name'] = split[0]\n"
                "df['company_rating'] = split[1]\n"
                "df = df.drop(columns=['Company Name'])\n"
                "df.to_csv(path, index=False)\n"
            )
        else:
            # Any other issue (there are none in this fixture) — a harmless no-op.
            code = f"import pandas as pd\npd.read_csv({file_path!r}, dtype=str).to_csv({file_path!r}, index=False)\n"
        return _FakeResponse(code)


N = 25
company_values = [f"Company{i}\n{2.5 + (i % 20) * 0.1:.1f}" for i in range(N - 1)] + ["Anomaly Corp"]
job_titles = [f"Data Scientist {i}" for i in range(N)]

print("=" * 70)
print("TEST 1 (end-to-end via clean_dataset): composite-field split")
print("=" * 70)

tmp_dir = Path(tempfile.mkdtemp(prefix="composite_split_"))
try:
    fixture_path = tmp_dir / "jobs.csv"
    fixture_df = pd.DataFrame({"Company Name": company_values, "Job Title": job_titles})
    fixture_df.to_csv(fixture_path, index=False)

    fake_llm = CompositeFixLLM()
    stdout_capture = StringIO()
    original_stdout = sys.stdout
    sys.stdout = stdout_capture
    try:
        with redirect_stdin_yes(count=dc.MAX_CLEAN_ATTEMPTS + 2):
            result = dc.clean_dataset(str(tmp_dir), llm=fake_llm)
    finally:
        sys.stdout = original_stdout
    printed = stdout_capture.getvalue()
    print(printed[-4000:])  # only tail, to keep this test's own output readable

    assert result.cleaned_files, f"expected the file to be cleaned, got: {result.summary()}"
    file_record = result.cleaned_files[0]

    composite_records = [
        r for r in file_record.fail_issue_records
        if r.issue.startswith("Composite field (discovered):")
    ]
    assert len(composite_records) == 1, (
        f"expected exactly 1 composite-field fail issue (the discovery pass flagged it), "
        f"got: {file_record.fail_issue_records}"
    )
    assert composite_records[0].status == "resolved", (
        f"expected the composite split to resolve, got status={composite_records[0].status!r}, "
        f"error={composite_records[0].error!r}"
    )
    print("PASS: discovery flagged the composite-field issue and the scoped fix resolved it.\n")

    assert "GENERATED CLEANING CODE for:" in printed, (
        "the approval gate (_request_approval) must still have been invoked and printed "
        "the generated code for real human review"
    )
    assert "COMPOSITE_FIELD_SPLIT" not in printed  # sanity: we're checking the gate fired, not prompt internals
    print("PASS: the human approval gate was actually invoked (not bypassed).\n")

    result_df = pd.read_csv(fixture_path.parent / "cleaned" / "jobs.csv", dtype=str)
    print("result columns:", list(result_df.columns))
    assert "Company Name" not in result_df.columns, "original composite column must be dropped"
    new_columns = [c for c in result_df.columns if c not in ("Job Title",)]
    assert set(new_columns) == {"company_name", "company_rating"}, (
        f"expected exactly the two replacement columns, got {new_columns}"
    )
    print("PASS: original composite column is gone; exactly two replacement columns exist.\n")

    for i in range(N - 1):
        assert result_df.loc[i, "company_name"] == f"Company{i}", result_df.loc[i]
        assert result_df.loc[i, "company_rating"] == f"{2.5 + (i % 20) * 0.1:.1f}", result_df.loc[i]
    print(f"PASS: all {N - 1} matching rows were correctly split into name + rating.\n")

    last_row = result_df.iloc[N - 1]
    assert pd.isna(last_row["company_name"]) and pd.isna(last_row["company_rating"]), (
        f"the one deliberately non-matching row must be null in both new columns, got {last_row}"
    )
    print("PASS: the non-matching row is null in both new columns, not fabricated.\n")

    print("=" * 70)
    print("TEST 2: every other column is byte-for-byte unchanged")
    print("=" * 70)
    assert result_df["Job Title"].tolist() == job_titles, (
        "Job Title must be completely untouched by the composite-field split"
    )
    print("PASS: 'Job Title' is byte-for-byte identical to the original.\n")
finally:
    shutil.rmtree(tmp_dir, ignore_errors=True)


print("=" * 70)
print("TEST 3 (test double): a fix that drops the flagged column with NO")
print("replacement columns is caught by the post-fix shape check")
print("=" * 70)


class DropOnlyLLM:
    """A composite-split 'fix' that just drops the flagged column and adds nothing —
    the exact failure mode the shape check exists to catch. check_rubric()'s own
    re-check has nothing to say here (it never produces a 'Composite field
    (discovered):' string at all), so without the shape check this would silently
    be accepted as 'resolved'."""

    def invoke(self, messages):
        human_text = messages[-1][1]
        match = _FILE_PATH_RE.search(human_text)
        file_path = match.group(1).strip()
        # errors='ignore': _clean_issue_group retries against the SAME (already-
        # modified) file on each attempt, so this must stay a no-op once the column
        # is already gone from a prior attempt, rather than crashing on retry.
        code = (
            "import pandas as pd\n"
            f"path = {file_path!r}\n"
            "df = pd.read_csv(path, dtype=str)\n"
            "df = df.drop(columns=['Company Name'], errors='ignore')\n"  # dropped, nothing added back
            "df.to_csv(path, index=False)\n"
        )
        return _FakeResponse(code)


tmp_dir2 = Path(tempfile.mkdtemp(prefix="composite_split_dropfail_"))
try:
    cloned_path = tmp_dir2 / "jobs.csv"
    fixture_df2 = pd.DataFrame({"Company Name": company_values, "Job Title": job_titles})
    fixture_df2.to_csv(cloned_path, index=False)

    composite_issue = (
        "Composite field (discovered): column 'Company Name' has 24 value(s) (96%) "
        f"matching the pattern '{COMPOSITE_REGEX}' — this looks like two distinct values "
        "glued together, not caught by a fixed rubric check."
    )
    pattern_lookup = {
        composite_issue: {
            "issue": composite_issue, "pattern": COMPOSITE_REGEX, "match_threshold": 0.96,
        }
    }

    with redirect_stdin_yes(count=dc.MAX_CLEAN_ATTEMPTS + 2):
        status, attempts, error, remaining, code = dc._clean_issue_group(
            cloned_path, [composite_issue], DropOnlyLLM(), pattern_lookup=pattern_lookup
        )
    print(f"status={status!r} attempts={attempts} error={error!r}")

    assert status == "skipped_incomplete", (
        f"a drop-with-no-replacement fix must be caught, not accepted as 'resolved', got {status!r}"
    )
    assert error == "Expected exactly 2 replacement columns after composite-field split, found 0.", (
        f"expected the exact required reason string, got: {error!r}"
    )
    assert remaining == [composite_issue], f"expected the issue to remain unresolved, got {remaining}"
    print("PASS: the post-fix shape check caught the drop-with-no-replacement fix, marked "
          "'skipped_incomplete' with the exact expected reason string.\n")
finally:
    shutil.rmtree(tmp_dir2, ignore_errors=True)


print("=" * 70)
print("TEST 4: a missing pattern_lookup entry falls back to")
print("_generate_cleaning_code, with a logged warning")
print("=" * 70)


class GenericFallbackLLM:
    """A plain cleaning-code generator (the normal, non-composite path) — used to
    confirm the fallback actually routes here, not to _generate_composite_split_code."""

    def invoke(self, messages):
        assert messages[0][1] == dc.CLEANING_CODE_SYSTEM_PROMPT, (
            "fallback must use the GENERAL-PURPOSE system prompt, not the composite one"
        )
        human_text = messages[-1][1]
        match = re.search(r"exact path\): (.+)", human_text)
        file_path = match.group(1).strip()
        # A harmless no-op — this test only cares about which generator got called.
        code = f"import pandas as pd\npd.read_csv({file_path!r}, dtype=str).to_csv({file_path!r}, index=False)\n"
        return _FakeResponse(code)


tmp_dir3 = Path(tempfile.mkdtemp(prefix="composite_split_missing_pattern_"))
try:
    cloned_path3 = tmp_dir3 / "jobs.csv"
    fixture_df3 = pd.DataFrame({"Company Name": company_values, "Job Title": job_titles})
    fixture_df3.to_csv(cloned_path3, index=False)

    composite_issue3 = (
        "Composite field (discovered): column 'Company Name' has 24 value(s) (96%) "
        f"matching the pattern '{COMPOSITE_REGEX}' — this looks like two distinct values "
        "glued together, not caught by a fixed rubric check."
    )
    fallback_llm = GenericFallbackLLM()

    stderr_capture = StringIO()
    with redirect_stderr(stderr_capture):
        with redirect_stdin_yes(count=dc.MAX_CLEAN_ATTEMPTS + 2):
            status, attempts, error, remaining, code = dc._clean_issue_group(
                cloned_path3, [composite_issue3], fallback_llm, pattern_lookup={}
            )
    stderr_text = stderr_capture.getvalue()
    print("stderr:", stderr_text.strip())
    print(f"status={status!r}")

    assert "No verified pattern found" in stderr_text, (
        f"expected a logged warning about the missing pattern lookup, got: {stderr_text!r}"
    )
    # Confirms _generate_cleaning_code (not _generate_composite_split_code) was actually
    # used: GenericFallbackLLM's own assertion above would have raised otherwise, and
    # the no-op script leaves 'Company Name' in place, so check_rubric's re-check still
    # trivially treats the (undetectable) issue as gone but the FILE was never split.
    result_df3 = pd.read_csv(cloned_path3, dtype=str)
    assert "Company Name" in result_df3.columns, (
        "the fallback general-purpose generator must not have performed a composite split"
    )
    print("PASS: a missing pattern_lookup entry falls back to the general-purpose "
          "generator (never raises), with a visible logged warning.\n")
finally:
    shutil.rmtree(tmp_dir3, ignore_errors=True)


print("=" * 70)
print("ALL COMPOSITE-FIELD-SPLIT ASSERTIONS PASSED")
print("=" * 70)
