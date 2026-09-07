"""
Spec 1, Part 7: feature engineering — judgment-call derived columns
(utils/feature_derivation.py), structurally separate from clean_dataset().

Test 1: detect_feature_derivation_candidates finds all 5 real candidates
against the actual Uncleaned_DS_jobs.csv (job title categorization, company
age, same-state flag, skill keywords, seniority flag) — none of them
hallucinated, each backed by real column names/sample values/percentages.
Test 2: a dataset with none of the needed source columns produces zero
candidates (no false positives).
Test 3: derive_features end-to-end for "company_age" — both missing-Founded
sub-choices ("null" vs "age_zero") produce different, correct results, and
the shape check enforces exactly one new column, original preserved.
Test 4: derive_features end-to-end for "skill_keywords" — multiple new
boolean flag columns are all added together in one cycle; the shape check
rejects a fix that adds the wrong set of columns.
Test 5: a legitimate NO_DERIVATION decline is a terminal, successful outcome
— no retry, no approval prompt, same discipline as Spec 2.1 (never fed into
the retry loop, so no fabrication-under-pressure path).
"""

import shutil
import sys
import tempfile
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pandas as pd

import utils.data_cleaning as dc
import utils.feature_derivation as fd


@contextmanager
def redirect_stdin_yes(count=10):
    original_stdin = sys.stdin
    sys.stdin = StringIO("yes\n" * count)
    try:
        yield
    finally:
        sys.stdin = original_stdin


@contextmanager
def redirect_stdin_none():
    """A non-tty-shaped, empty stdin — used to prove a legitimate decline
    never reaches _request_approval (an accidental call would raise EOFError,
    same convention as test_composite_field_decline.py)."""
    original_stdin = sys.stdin
    sys.stdin = StringIO("")
    try:
        yield
    finally:
        sys.stdin = original_stdin


class _FakeResponse:
    def __init__(self, content):
        self.content = content


def _real_path(human_text: str) -> str:
    import re
    m = re.search(r"exact path\): (.+)", human_text)
    return m.group(1).strip()


print("=" * 70)
print("TEST 1: detect_feature_derivation_candidates finds all 5 real")
print("candidates against the real dataset, each backed by real facts")
print("=" * 70)

real_df = dc._read_csv_robust("data/data-science-jobs/Uncleaned_DS_jobs.csv")
real_candidates = fd.detect_feature_derivation_candidates(real_df)
by_kind = {c["kind"]: c for c in real_candidates}
assert set(by_kind) == set(fd.FEATURE_DERIVATION_KINDS), (
    f"expected all 5 kinds detected, got {set(by_kind)}"
)
assert by_kind["job_title_categorization"]["columns"] == ["Job Title"]
assert by_kind["company_age"]["columns"] == ["Founded"]
assert by_kind["same_state_flag"]["columns"] == ["Location", "Headquarters"]
assert by_kind["skill_keywords"]["columns"] == ["Job Description"]
assert by_kind["seniority_flag"]["columns"] == ["Job Title"]
for kind, cand in by_kind.items():
    assert cand["description"], f"{kind} candidate must have a real, non-empty description"
print("PASS: all 5 candidates detected with the correct real source column(s).\n")

print("=" * 70)
print("TEST 2: a dataset with none of the needed source columns")
print("produces zero candidates (no false positives)")
print("=" * 70)

empty_df = pd.DataFrame({"amount": ["10", "20"], "currency": ["USD", "EUR"]})
assert fd.detect_feature_derivation_candidates(empty_df) == [], (
    "expected zero candidates for a dataset with no matching source columns"
)
print("PASS: no false positives on an unrelated dataset.\n")

print("=" * 70)
print("TEST 3: derive_features('company_age') — both missing-Founded")
print("sub-choices produce different, correct results")
print("=" * 70)


class CompanyAgeLLM:
    def __init__(self, missing_choice):
        self.missing_choice = missing_choice
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        human_text = messages[-1][1]
        path = _real_path(human_text)
        fill = "0" if self.missing_choice == "age_zero" else "None"
        code = (
            "import pandas as pd\n"
            f"path = {path!r}\n"
            "df = pd.read_csv(path, dtype=str)\n"
            "year = pd.to_numeric(df['Founded'], errors='coerce')\n"
            "year = year.where(year > 0)  # treat -1 placeholder as missing too\n"
            "age = 2026 - year\n"
            f"df['company_age'] = age.where(age.notna(), {fill})"
            f"  # deliberate assumption: missing Founded -> {self.missing_choice}\n"
            "df.to_csv(path, index=False)\n"
        )
        return _FakeResponse(code)


founded_values = ["1993", "-1", "2010", "-1", "2000"]
job_titles = [f"Role {i}" for i in range(len(founded_values))]

for missing_choice, expected_missing_age in (("null", None), ("age_zero", 0)):
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"feature_company_age_{missing_choice}_"))
    try:
        file_path = tmp_dir / "jobs.csv"
        pd.DataFrame({"Founded": founded_values, "Job Title": job_titles}).to_csv(file_path, index=False)

        spec = {"kind": "company_age", "columns": ["Founded"], "missing_founded_choice": missing_choice}
        fake_llm = CompanyAgeLLM(missing_choice)
        with redirect_stdin_yes():
            result = fd.derive_features(file_path, spec, llm=fake_llm)

        assert result["status"] == "resolved", f"[{missing_choice}] expected resolved, got {result}"
        assert result["new_columns"] == ["company_age"], result["new_columns"]

        out = pd.read_csv(file_path, dtype=str)
        assert out["Founded"].tolist() == founded_values, "original column must be byte-for-byte unchanged"
        out = pd.read_csv(file_path)  # re-read normally for the numeric company_age assertions below
        assert out.loc[0, "company_age"] == 2026 - 1993
        assert out.loc[2, "company_age"] == 2026 - 2010
        if expected_missing_age is None:
            assert pd.isna(out.loc[1, "company_age"]) and pd.isna(out.loc[3, "company_age"]), (
                f"[null choice] expected missing Founded -> null company_age, got {out}"
            )
        else:
            assert out.loc[1, "company_age"] == 0 and out.loc[3, "company_age"] == 0, (
                f"[age_zero choice] expected missing Founded -> company_age 0, got {out}"
            )
        print(f"PASS: missing_founded_choice='{missing_choice}' produces the correct, distinct result.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
print()

print("=" * 70)
print("TEST 3b: shape check rejects a fix that adds the wrong column name")
print("(repeatedly, without corrupting the file further each retry)")
print("=" * 70)

tmp_dir3b = Path(tempfile.mkdtemp(prefix="feature_company_age_badshape_"))
try:
    file_path3b = tmp_dir3b / "jobs.csv"
    pd.DataFrame({"Founded": founded_values, "Job Title": job_titles}).to_csv(file_path3b, index=False)

    class WrongColumnNameLLM:
        """Idempotent, shape-violating fix: adds a column under the WRONG name
        every attempt (never 'company_age') without touching or losing
        'Founded' — safe to run more than once, unlike a fix that drops the
        original (which would crash on a second attempt once already gone)."""

        def __init__(self):
            self.call_count = 0

        def invoke(self, messages):
            self.call_count += 1
            path = _real_path(messages[-1][1])
            code = (
                "import pandas as pd\n"
                f"path = {path!r}\n"
                "df = pd.read_csv(path, dtype=str)\n"
                "if 'age_of_company' not in df.columns:\n"
                "    df['age_of_company'] = 5\n"  # wrong name — violates the fixed expected-column contract
                "df.to_csv(path, index=False)\n"
            )
            return _FakeResponse(code)

    fake_llm3b = WrongColumnNameLLM()
    spec3b = {"kind": "company_age", "columns": ["Founded"]}
    with redirect_stdin_yes(count=dc.MAX_CLEAN_ATTEMPTS + 2):
        result3b = fd.derive_features(file_path3b, spec3b, llm=fake_llm3b)

    assert result3b["status"] == "skipped_incomplete", f"expected skipped_incomplete, got {result3b}"
    assert "company_age" in result3b["error"], (
        f"expected the shape-check reason to name the expected column, got {result3b}"
    )
    assert fake_llm3b.call_count == dc.MAX_CLEAN_ATTEMPTS, (
        f"expected the shape-check failure fed back and retried up to the cap, got {fake_llm3b.call_count}"
    )
    out3b = pd.read_csv(file_path3b, dtype=str)
    assert out3b["Founded"].tolist() == founded_values, "original column must survive even a rejected fix"
    print(f"PASS: the wrong column name is rejected every attempt, capped at "
          f"{dc.MAX_CLEAN_ATTEMPTS} tries, original column intact: {result3b['error']}\n")
finally:
    shutil.rmtree(tmp_dir3b, ignore_errors=True)

print("=" * 70)
print("TEST 4: derive_features('skill_keywords') — multiple new boolean")
print("columns added together; wrong column set is rejected")
print("=" * 70)


class SkillKeywordsLLM:
    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        path = _real_path(messages[-1][1])
        code = (
            "import pandas as pd\n"
            f"path = {path!r}\n"
            "df = pd.read_csv(path, dtype=str)\n"
            "text = df['Job Description'].fillna('').str.lower()\n"
            "df['has_python'] = text.str.contains('python')\n"
            "df['has_excel'] = text.str.contains('excel')\n"
            "df['has_hadoop'] = text.str.contains('hadoop')\n"
            "df['has_spark'] = text.str.contains('spark')\n"
            "df['has_aws'] = text.str.contains('aws')\n"
            "df['has_tableau'] = text.str.contains('tableau')\n"
            "df['has_big_data'] = text.str.contains('big data')\n"
            "df.to_csv(path, index=False)\n"
        )
        return _FakeResponse(code)


descriptions = [
    "Must know Python and Excel for this role.",
    "Experience with Spark and AWS required. Big Data pipelines a plus.",
    "General office duties, no technical skills needed.",
]

tmp_dir4 = Path(tempfile.mkdtemp(prefix="feature_skill_keywords_"))
try:
    file_path4 = tmp_dir4 / "jobs.csv"
    pd.DataFrame({"Job Description": descriptions}).to_csv(file_path4, index=False)

    spec4 = {"kind": "skill_keywords", "columns": ["Job Description"]}
    fake_llm4 = SkillKeywordsLLM()
    with redirect_stdin_yes():
        result4 = fd.derive_features(file_path4, spec4, llm=fake_llm4)

    assert result4["status"] == "resolved", f"expected resolved, got {result4}"
    assert set(result4["new_columns"]) == set(fd.FEATURE_EXPECTED_NEW_COLUMNS["skill_keywords"]), (
        result4["new_columns"]
    )

    out4 = pd.read_csv(file_path4)
    assert out4.loc[0, "has_python"] and out4.loc[0, "has_excel"]
    assert not out4.loc[0, "has_spark"]
    assert out4.loc[1, "has_spark"] and out4.loc[1, "has_aws"] and out4.loc[1, "has_big_data"]
    assert not any(out4.loc[2, c] for c in fd.FEATURE_EXPECTED_NEW_COLUMNS["skill_keywords"])
    print("PASS: all 7 skill-keyword flag columns added together with correct per-row values.\n")
finally:
    shutil.rmtree(tmp_dir4, ignore_errors=True)

print("=" * 70)
print("TEST 5: a legitimate NO_DERIVATION decline is terminal and successful —")
print("no retry, no approval prompt reached at all")
print("=" * 70)


class AlwaysDeclineFeatureLLM:
    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        return _FakeResponse(
            "# NO_DERIVATION: this column is already a small fixed set of categories, "
            "not free-text job titles worth categorizing."
        )


tmp_dir5 = Path(tempfile.mkdtemp(prefix="feature_decline_"))
try:
    file_path5 = tmp_dir5 / "jobs.csv"
    pd.DataFrame({"Job Title": ["A", "B", "A", "B"]}).to_csv(file_path5, index=False)

    fake_llm5 = AlwaysDeclineFeatureLLM()
    spec5 = {"kind": "job_title_categorization", "columns": ["Job Title"]}
    with redirect_stdin_none():  # an accidental _request_approval call would raise EOFError
        result5 = fd.derive_features(file_path5, spec5, llm=fake_llm5)

    assert result5["status"] == "declined_false_positive", f"expected a decline, got {result5}"
    assert fake_llm5.call_count == 1, f"expected exactly 1 LLM call, no retry, got {fake_llm5.call_count}"
    out5 = pd.read_csv(file_path5)
    assert list(out5.columns) == ["Job Title"], "the file must be completely untouched on a decline"
    print(f"PASS: decline is terminal after exactly 1 call, never reaches approval: {result5['error']}\n")
finally:
    shutil.rmtree(tmp_dir5, ignore_errors=True)

print("=" * 70)
print("ALL FEATURE-DERIVATION (SPEC 1, PART 7) ASSERTIONS PASSED")
print("=" * 70)
