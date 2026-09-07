"""
Spec 1, Part 8: verbose categorical label simplification (e.g. "51 to 200
employees" -> "51-200") — purely a style choice, nothing objectively wrong,
so it's kept out of check_rubric's issue list and offered only through
Transformation Options.

Test 1: _detect_label_simplification_columns finds the real 'Size' column
on the actual Uncleaned_DS_jobs.csv, correctly excluding '-1'/'Unknown'
placeholder-like values from counting against the match fraction... (they
count against it, but the fraction still clears threshold); a short,
unrelated text column is not flagged.
Test 2: end-to-end simplify_labels — a real, working simplification script
shortens matching values in place (no new column, same column count),
preserves non-matching placeholder values untouched, and the shape check
rejects a fix that adds a new column instead of rewriting in place.
Test 3: a legitimate NO_SIMPLIFICATION decline is terminal — no retry, no
approval prompt reached.
Test 4: relevance-tag wiring — utils.transformation_options wraps this
detector into the shared TransformationCandidate shape with column-derived
tags, and surface_relevant_transformations only offers it when the column is
part of the current question's category/grouping dimension (via
chart_category_column), not proactively for an unrelated question.
"""

import shutil
import sys
import tempfile
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pandas as pd

import utils.data_cleaning as dc
import utils.transformation_options as topt


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
print("TEST 1: _detect_label_simplification_columns — real 'Size' column")
print("detected; a short unrelated column is not")
print("=" * 70)

real_df = dc._read_csv_robust("data/data-science-jobs/Uncleaned_DS_jobs.csv")
real_candidates = dc._detect_label_simplification_columns(real_df)
by_col = {c["column"]: c for c in real_candidates}
assert "Size" in by_col, f"expected 'Size' detected, got {real_candidates}"
assert by_col["Size"]["match_fraction"] >= dc.LABEL_SIMPLIFICATION_MATCH_THRESHOLD
assert "employees" in by_col["Size"]["sample_value"]
print(f"PASS: {by_col['Size']}\n")

unrelated_df = pd.DataFrame({"industry": ["Tech", "Finance", "Healthcare", "Retail"]})
assert dc._detect_label_simplification_columns(unrelated_df) == [], (
    "an unrelated categorical column with no verbose numeric-range shape must not be flagged"
)
print("PASS: an unrelated categorical column is correctly not flagged.\n")

print("=" * 70)
print("TEST 2: simplify_labels — real in-place rewrite, placeholders")
print("untouched, shape check rejects a fix that adds a column instead")
print("=" * 70)


class SizeSimplifyLLM:
    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        path = _real_path(messages[-1][1])
        code = (
            "import pandas as pd\n"
            "import re\n"
            f"path = {path!r}\n"
            "df = pd.read_csv(path, dtype=str)\n"
            "def simplify(v):\n"
            "    if not isinstance(v, str):\n"
            "        return v\n"
            "    m = re.match(r'^(\\d[\\d,]*)\\+\\s+employees$', v)\n"
            "    if m:\n"
            "        return m.group(1) + '+'\n"
            "    m = re.match(r'^(\\d[\\d,]*)\\s+to\\s+(\\d[\\d,]*)\\s+employees$', v)\n"
            "    if m:\n"
            "        return f'{m.group(1)}-{m.group(2)}'\n"
            "    return v\n"
            "df['Size'] = df['Size'].map(simplify)\n"
            "df.to_csv(path, index=False)\n"
        )
        return _FakeResponse(code)


size_values = [
    "1001 to 5000 employees", "51 to 200 employees", "10000+ employees",
    "-1", "Unknown",
]

tmp_dir2 = Path(tempfile.mkdtemp(prefix="label_simplify_"))
try:
    file_path2 = tmp_dir2 / "jobs.csv"
    pd.DataFrame({"Size": size_values, "Job Title": [f"Role {i}" for i in range(5)]}).to_csv(
        file_path2, index=False
    )

    candidate2 = {"column": "Size", "match_fraction": 0.6, "sample_value": "51 to 200 employees"}
    fake_llm2 = SizeSimplifyLLM()
    with redirect_stdin_yes():
        result2 = dc.simplify_labels(file_path2, candidate2, llm=fake_llm2)

    assert result2["status"] == "resolved", f"expected resolved, got {result2}"
    out2 = pd.read_csv(file_path2, dtype=str)
    assert list(out2.columns) == ["Size", "Job Title"], "no new column should be added"
    assert out2["Size"].tolist() == ["1001-5000", "51-200", "10000+", "-1", "Unknown"], out2["Size"].tolist()
    print("PASS: matching values simplified in place; placeholders/non-matching values untouched; "
          "no new column added.\n")
finally:
    shutil.rmtree(tmp_dir2, ignore_errors=True)

tmp_dir2b = Path(tempfile.mkdtemp(prefix="label_simplify_badshape_"))
try:
    file_path2b = tmp_dir2b / "jobs.csv"
    pd.DataFrame({"Size": size_values}).to_csv(file_path2b, index=False)

    class AddsColumnInsteadLLM:
        def __init__(self):
            self.call_count = 0

        def invoke(self, messages):
            self.call_count += 1
            path = _real_path(messages[-1][1])
            code = (
                "import pandas as pd\n"
                f"path = {path!r}\n"
                "df = pd.read_csv(path, dtype=str)\n"
                "df['Size_simplified'] = df['Size']\n"  # violates "rewrite in place, add nothing"
                "df.to_csv(path, index=False)\n"
            )
            return _FakeResponse(code)

    fake_llm2b = AddsColumnInsteadLLM()
    candidate2b = {"column": "Size", "match_fraction": 0.6, "sample_value": "51 to 200 employees"}
    with redirect_stdin_yes(count=dc.MAX_CLEAN_ATTEMPTS + 2):
        result2b = dc.simplify_labels(file_path2b, candidate2b, llm=fake_llm2b)

    assert result2b["status"] == "skipped_incomplete", f"expected skipped_incomplete, got {result2b}"
    assert fake_llm2b.call_count == dc.MAX_CLEAN_ATTEMPTS
    print(f"PASS: a fix that adds a column instead of rewriting in place is rejected: "
          f"{result2b['error']}\n")
finally:
    shutil.rmtree(tmp_dir2b, ignore_errors=True)

print("=" * 70)
print("TEST 3: a legitimate NO_SIMPLIFICATION decline is terminal —")
print("no retry, no approval prompt reached")
print("=" * 70)


class AlwaysDeclineLabelLLM:
    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        return _FakeResponse(
            "# NO_SIMPLIFICATION: these values are already short category codes, not "
            "verbose numeric-range labels."
        )


tmp_dir3 = Path(tempfile.mkdtemp(prefix="label_simplify_decline_"))
try:
    file_path3 = tmp_dir3 / "jobs.csv"
    pd.DataFrame({"Size": ["A", "B", "A"]}).to_csv(file_path3, index=False)

    fake_llm3 = AlwaysDeclineLabelLLM()
    candidate3 = {"column": "Size", "match_fraction": 0.9, "sample_value": "A"}
    with redirect_stdin_none():
        result3 = dc.simplify_labels(file_path3, candidate3, llm=fake_llm3)

    assert result3["status"] == "declined_false_positive", f"expected a decline, got {result3}"
    assert fake_llm3.call_count == 1, f"expected exactly 1 LLM call, got {fake_llm3.call_count}"
    out3 = pd.read_csv(file_path3, dtype=str)
    assert out3["Size"].tolist() == ["A", "B", "A"], "the file must be completely untouched on a decline"
    print(f"PASS: decline is terminal after exactly 1 call: {result3['error']}\n")
finally:
    shutil.rmtree(tmp_dir3, ignore_errors=True)

print("=" * 70)
print("TEST 4: relevance-tag wiring — offered only when the column is part of")
print("the current question's category/grouping dimension")
print("=" * 70)

wrapped = topt._detect_label_simplification_wrapped_candidates(real_df, "jobs_table")
by_kind_col = {(c.kind, tuple(c.columns)): c for c in wrapped}
size_candidate = by_kind_col.get(("label_simplification", ("Size",)))
assert size_candidate is not None, f"expected a wrapped Size candidate, got {wrapped}"
assert "size" in size_candidate.relevance_tags, size_candidate.relevance_tags

relevant_for_size_question = topt.surface_relevant_transformations(
    curated_question="How many jobs are there by company size?",
    chart_category_column="size",
    chart_value_column="job_count",
    touched_columns=["size"],
    candidates=[size_candidate],
)
assert size_candidate in relevant_for_size_question, "expected surfaced when Size is the category dimension"

relevant_for_unrelated_question = topt.surface_relevant_transformations(
    curated_question="What is the average rating by industry?",
    chart_category_column="industry",
    chart_value_column="avg_rating",
    touched_columns=["industry", "rating"],
    candidates=[size_candidate],
)
assert size_candidate not in relevant_for_unrelated_question, (
    "expected NOT surfaced for a question that doesn't touch Size at all"
)
print("PASS: label-simplification candidate surfaces only for a question whose category/"
      "grouping dimension is the flagged column, not proactively for an unrelated question.\n")

print("=" * 70)
print("ALL LABEL-SIMPLIFICATION (SPEC 1, PART 8) ASSERTIONS PASSED")
print("=" * 70)
