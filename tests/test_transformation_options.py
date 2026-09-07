"""
Spec 1, Parts 0 / 0.5 / 1 — Transformation Options: unified candidate
detection, question-driven surfacing, and the decision cache/presentation
function.

Test 1: TransformationCandidate to_dict/from_dict round trip.
Test 2: compute_candidate_id is deterministic — same (table, kind, columns)
(in any column order) always produces the same id, including across a fresh
Python process (the actual property the spec's own reasoning depends on —
see compute_candidate_id's docstring for why plain hash() would NOT have
this property); a different table/kind/column produces a different id.
Test 3: detect_transformation_candidates wraps the existing range-
decomposition detector into the shared shape, with real match-fraction/
sample-value facts in the description and column-name-derived relevance
tags (salary vs. revenue).
Test 4: surface_relevant_transformations — column match (including a raw-
CSV-name vs. sanitized-DB-column-name spelling difference) and tag match
(a question naming the topic without the exact column touched) each
independently surface a candidate; an unrelated candidate is filtered out.
Test 5: present_transformation_options — blocks on real input() (piped
stdin), retries on an invalid answer, always offers the implicit skip
option, and persists the full decision (context + options shown) to
_transformation_decisions.
Test 6: the decision cache round-trips through Postgres, and
invalidate_cached_decisions_for_table genuinely clears a cached decision —
the reload-invalidation rule from Part 0.

Requires a live Postgres reachable via utils.load_data.get_admin_connection()
(same as tests/test_fanout_status.py) — no LLM involved anywhere in this file.
"""

import subprocess
import sys
from contextlib import contextmanager
from io import StringIO

import pandas as pd

from utils.load_data import (
    ensure_transformation_candidates_table,
    ensure_transformation_decisions_table,
    get_admin_connection,
    invalidate_cached_decisions_for_table,
    read_transformation_candidates,
    read_transformation_decision,
    write_transformation_candidates,
)
from utils.transformation_options import (
    TransformationCandidate,
    compute_candidate_id,
    detect_transformation_candidates,
    present_transformation_options,
    surface_relevant_transformations,
)


@contextmanager
def redirect_stdin(text: str):
    original_stdin = sys.stdin
    sys.stdin = StringIO(text)
    try:
        yield
    finally:
        sys.stdin = original_stdin


print("=" * 70)
print("TEST 1: TransformationCandidate to_dict/from_dict round trip")
print("=" * 70)

cand1 = TransformationCandidate(
    candidate_id="abc123",
    kind="range_decomposition",
    columns=["Salary Estimate"],
    description="80% match, sample '$100K-$150K'",
    relevance_tags=["salary", "compensation"],
)
round_tripped = TransformationCandidate.from_dict(cand1.to_dict())
assert round_tripped == cand1, f"round trip mismatch: {round_tripped} != {cand1}"
print("PASS: to_dict/from_dict round trip preserves every field.\n")

print("=" * 70)
print("TEST 2: compute_candidate_id determinism")
print("=" * 70)

id_a = compute_candidate_id("jobs", "range_decomposition", ["Salary Estimate"])
id_b = compute_candidate_id("jobs", "range_decomposition", ["Salary Estimate"])
assert id_a == id_b, "same inputs must always produce the same id"

# Column order must not matter — spec: tuple(sorted(columns)).
id_multi_a = compute_candidate_id("jobs", "feature_derivation", ["col_x", "col_y"])
id_multi_b = compute_candidate_id("jobs", "feature_derivation", ["col_y", "col_x"])
assert id_multi_a == id_multi_b, "column order must not affect the id"

# A different table/kind/column must produce a different id.
assert compute_candidate_id("other_table", "range_decomposition", ["Salary Estimate"]) != id_a
assert compute_candidate_id("jobs", "label_simplification", ["Salary Estimate"]) != id_a
assert compute_candidate_id("jobs", "range_decomposition", ["Revenue"]) != id_a

# Cross-process stability: the whole point of a durable decision cache (Part 1) is
# that the SAME candidate gets the SAME id in a totally separate process/session —
# this is exactly the property plain Python hash() would NOT have (PYTHONHASHSEED
# randomizes str hashing per-process by default).
subprocess_id = subprocess.run(
    [sys.executable, "-c",
     "from utils.transformation_options import compute_candidate_id; "
     "print(compute_candidate_id('jobs', 'range_decomposition', ['Salary Estimate']))"],
    cwd=".", capture_output=True, text=True, check=True,
).stdout.strip()
assert subprocess_id == id_a, (
    f"candidate_id must be stable across process restarts, got {subprocess_id!r} != {id_a!r}"
)
print(f"PASS: candidate_id is deterministic, order-independent, and stable across a fresh "
      f"process ({id_a}).\n")

print("=" * 70)
print("TEST 3: detect_transformation_candidates wraps range decomposition")
print("into the shared TransformationCandidate shape")
print("=" * 70)

df3 = pd.DataFrame({
    "Salary Estimate": ["$100K-$150K (Glassdoor est.)"] * 8 + ["n/a"],
    "Revenue": ["$1 to $2 billion (USD)"] * 8 + ["Unknown"],
    "Job Title": ["Data Scientist"] * 9,
})
candidates3 = detect_transformation_candidates(df3, "jobs_table")
by_kind_col = {(c.kind, tuple(c.columns)): c for c in candidates3}
salary_cand = by_kind_col.get(("range_decomposition", ("Salary Estimate",)))
revenue_cand = by_kind_col.get(("range_decomposition", ("Revenue",)))
assert salary_cand is not None, f"expected a Salary Estimate range candidate, got {candidates3}"
assert revenue_cand is not None, f"expected a Revenue range candidate, got {candidates3}"
assert "salary" in salary_cand.relevance_tags and "compensation" in salary_cand.relevance_tags
assert "revenue" in revenue_cand.relevance_tags or "income" in revenue_cand.relevance_tags
assert "100" in salary_cand.description or "%" in salary_cand.description, (
    "description must contain real, mechanically-observed facts (match fraction/sample), "
    f"got: {salary_cand.description}"
)
assert salary_cand.candidate_id == compute_candidate_id(
    "jobs_table", "range_decomposition", ["Salary Estimate"]
)
print("PASS: range-decomposition candidates detected with correct tags/description/candidate_id.\n")

print("=" * 70)
print("TEST 4: surface_relevant_transformations — column match and tag match,")
print("generic across candidate kinds")
print("=" * 70)

salary_candidate = TransformationCandidate(
    candidate_id="cand-salary",
    kind="range_decomposition",
    columns=["Salary Estimate"],
    description="salary range candidate",
    relevance_tags=["salary", "compensation"],
)
company_age_candidate = TransformationCandidate(
    candidate_id="cand-age",
    kind="feature_derivation",
    columns=["company_age"],
    description="derive company age from Founded",
    relevance_tags=["company age", "founding", "tenure"],
)
same_state_candidate = TransformationCandidate(
    candidate_id="cand-state",
    kind="feature_derivation",
    columns=["same_state_flag"],
    description="same-state flag from Location/Headquarters",
    relevance_tags=["location", "headquarters", "geography"],
)

all_candidates = [salary_candidate, company_age_candidate, same_state_candidate]

# A salary/job-title question, with the DB's sanitized column name in touched_columns
# (not the raw CSV spelling "Salary Estimate") — column match must tolerate that.
relevant_salary = surface_relevant_transformations(
    curated_question="What are the highest-paying job titles?",
    chart_category_column="job_title",
    chart_value_column="avg_salary_estimate",
    touched_columns=["job_title", "salary_estimate"],
    candidates=all_candidates,
)
assert salary_candidate in relevant_salary, "salary candidate must surface via column match"
assert company_age_candidate not in relevant_salary, "company-age is irrelevant to this question"
assert same_state_candidate not in relevant_salary, "same-state is irrelevant to this question"
print("PASS: a salary/job-title question surfaces only the salary candidate (column match).\n")

# A question that names "company age" in the question text but whose touched_columns
# don't literally include the derived column yet (it doesn't exist until derived) —
# must surface via TAG match instead.
relevant_age = surface_relevant_transformations(
    curated_question="How does company age relate to average rating?",
    chart_category_column="",
    chart_value_column="avg_rating",
    touched_columns=["rating", "founded"],
    candidates=all_candidates,
)
assert company_age_candidate in relevant_age, "company-age candidate must surface via tag match"
assert salary_candidate not in relevant_age, "salary is irrelevant to this question"
assert same_state_candidate not in relevant_age, "same-state is irrelevant to this question"
print("PASS: a company-age question surfaces the company-age candidate via tag match, "
      "even without the derived column in touched_columns.\n")

print("=" * 70)
print("TEST 5: present_transformation_options — real input(), retry on invalid")
print("answer, implicit skip option, persists the full decision")
print("=" * 70)

conn5 = get_admin_connection()
try:
    ensure_transformation_decisions_table(conn5)
    invalidate_cached_decisions_for_table(conn5, "__test_transform_opts__")

    context5 = {
        "title": "Decompose Salary Estimate into min/max/avg?",
        "what_was_found": "100% of real values match a numeric range pattern.",
        "why_optional": "This is enrichment, not a correctness fix — reasonable to skip.",
    }
    options5 = [{"id": "apply", "label": "Apply now", "description": "Add min/max/avg columns."}]

    with redirect_stdin("bogus_answer\napply\n"):
        decision5 = present_transformation_options(
            table_name="__test_transform_opts__",
            candidate_id="cand-salary-test",
            context=context5,
            options=options5,
            conn=conn5,
        )
    assert decision5["chosen_option_id"] == "apply", (
        f"expected the retry to land on 'apply' after the invalid answer, got {decision5}"
    )
    assert decision5["reasoning_shown"]["context"] == context5
    shown_ids = {opt["id"] for opt in decision5["reasoning_shown"]["options"]}
    assert shown_ids == {"apply", "skip"}, f"expected an implicit skip option, got {shown_ids}"
    print("PASS: invalid answer triggered a retry; final choice + full context/options preserved.\n")

    stored = read_transformation_decision(conn5, "__test_transform_opts__", "cand-salary-test")
    assert stored is not None and stored["chosen_option_id"] == "apply", (
        f"expected the decision persisted to _transformation_decisions, got {stored}"
    )
    print("PASS: decision durably persisted to _transformation_decisions.\n")
finally:
    invalidate_cached_decisions_for_table(conn5, "__test_transform_opts__")
    conn5.close()

print("=" * 70)
print("TEST 6: decision cache round-trip + reload invalidation (Part 0/1)")
print("=" * 70)

conn6 = get_admin_connection()
try:
    ensure_transformation_candidates_table(conn6)
    ensure_transformation_decisions_table(conn6)
    table6 = "__test_transform_reload__"
    invalidate_cached_decisions_for_table(conn6, table6)

    write_transformation_candidates(conn6, table6, [salary_candidate, company_age_candidate])
    stored_candidates = read_transformation_candidates(conn6, table6)
    assert {c.candidate_id for c in stored_candidates} == {"cand-salary", "cand-age"}, (
        f"expected both candidates round-tripped, got {stored_candidates}"
    )
    print("PASS: write/read_transformation_candidates round-trips through Postgres.\n")

    with redirect_stdin("skip\n"):
        present_transformation_options(
            table_name=table6,
            candidate_id="cand-salary",
            context={"title": "t", "what_was_found": "f", "why_optional": "w"},
            options=[{"id": "apply", "label": "Apply", "description": "d"}],
            conn=conn6,
        )
    assert read_transformation_decision(conn6, table6, "cand-salary") is not None, (
        "expected the decision cached before simulating a reload"
    )

    # Simulate a genuine reload: re-detect (here, re-write the same candidates) and
    # invalidate — the cached decision must be gone afterward.
    write_transformation_candidates(conn6, table6, [salary_candidate, company_age_candidate])
    invalidate_cached_decisions_for_table(conn6, table6)
    assert read_transformation_decision(conn6, table6, "cand-salary") is None, (
        "expected the cached decision cleared by invalidate_cached_decisions_for_table on reload"
    )
    print("PASS: a genuine reload invalidates every cached decision for that table.\n")
finally:
    with conn6.cursor() as cur:
        cur.execute("DELETE FROM _transformation_candidates WHERE table_name = %s", (table6,))
    conn6.commit()
    invalidate_cached_decisions_for_table(conn6, table6)
    conn6.close()

print("=" * 70)
print("ALL TRANSFORMATION-OPTIONS (SPEC 1, PARTS 0/0.5/1) ASSERTIONS PASSED")
print("=" * 70)
