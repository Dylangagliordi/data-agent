"""
Spec 1 acceptance tests 1-2 (live graph wiring): surface_transformations
(agents/sql_analyst.py), the node inserted between add_context and
generate_sql/determine_chart_type, actually surfaces the right Transformation
Options candidates for a real question against the real uncleaned_ds_jobs
table, reuses cached decisions silently, and never re-asks.

Does NOT invoke generate_sql/is_safe/execute_sql (no LLM, no cost) — calls
add_context then surface_transformations directly, the same two nodes that
run consecutively in the real compiled graph before generate_sql. Forces
sys.stdin.isatty() -> True (surface_transformations' own fail-open-skip
check) via a monkeypatch, same convention as test_auto_clean_redirect.py's
`with patch("sys.stdin.isatty", ...)` — the real answers ("skip") must be
PIPED to this script's actual stdin at the shell level (a real, deliberately
non-tty pipe that input() can still read from), never swapped out for an
in-Python StringIO — swapping sys.stdin entirely would replace the very
object isatty() is patched on. Run as:
    printf 'skip\\n%.0s' {1..20} | PYTHONPATH=. uv run python tests/test_transformation_options_live_wiring.py

Test 1 (acceptance test 1): a question touching salary and job title surfaces
the range-decomposition (Salary Estimate), job-title-categorization, and
(Spec 3) categorical_consolidation (Job Title) candidates, but NOT
company-age, skill-keyword, or same-state-flag — confirmed by inspecting
exactly which "TRANSFORMATION OPTION:" banners are printed. All candidates
are answered "skip" so nothing is actually applied and the real table is
left untouched.

Test 2 (acceptance test 2): a second, different question mentioning "company
age" surfaces company-age (now, for the first time) while the salary/
job-title decisions from Test 1 are reused silently — their banners are NOT
printed again.
"""

import re
import sys
from io import StringIO
from unittest.mock import patch

from agents.sql_analyst import add_context, surface_transformations
from models.schema import SQLAnalystState
from utils.data_cleaning import _read_csv_robust
from utils.load_data import get_admin_connection, invalidate_cached_decisions_for_table, write_transformation_candidates
from utils.transformation_options import detect_transformation_candidates
from utils.load_data import read_transformation_candidates


def _offered_titles(captured_stdout: str) -> set:
    return set(re.findall(r"TRANSFORMATION OPTION: (.+)", captured_stdout))


TABLE = "uncleaned_ds_jobs"

conn = get_admin_connection()
try:
    invalidate_cached_decisions_for_table(conn, TABLE)
    original_candidates = read_transformation_candidates(conn, TABLE)  # restored in finally

    # Seed _transformation_candidates for the real table exactly the way a fresh
    # load/reload would (utils/load_data.py:main() / clean_and_reload) — this
    # table was loaded in an earlier session, before Part 0's detection wiring
    # existed, so it has no stored candidates yet; detection itself is already
    # covered by tests/test_transformation_options.py, this is just realistic
    # setup for exercising the live surfacing/decision-cache wiring below.
    # NOTE: the real cleaned/ artifact on disk for this table has already been
    # through this dev environment's own earlier composite-field-discovery runs
    # (which renamed/split "Salary Estimate", "Job Title", etc. into different
    # column names as a one-off side effect of prior sessions) — the RAW file
    # still has the original column names every detector here is keyed to, so
    # it's the representative stand-in for "a table right after a normal Phase
    # 1 clean" for this seeding step.
    raw_path = "data/data-science-jobs/Uncleaned_DS_jobs.csv"
    seed_df = _read_csv_robust(raw_path)
    write_transformation_candidates(conn, TABLE, detect_transformation_candidates(seed_df, TABLE))

    print("=" * 70)
    print("TEST 1: a salary + job-title question surfaces exactly those two")
    print("candidate kinds, not company-age/skill-keywords/same-state")
    print("=" * 70)

    state1 = SQLAnalystState(
        user_question="What is the average salary by job title?",
        curated_question="What is the average salary by job title?",
    )
    ctx1 = add_context(state1)
    state1_with_ctx = state1.model_copy(update=ctx1)

    captured1 = StringIO()
    with patch("sys.stdin.isatty", return_value=True):
        original_stdout = sys.stdout
        sys.stdout = captured1
        try:
            result1 = surface_transformations(state1_with_ctx)
        finally:
            sys.stdout = original_stdout
    printed1 = captured1.getvalue()
    offered1 = _offered_titles(printed1)
    print("offered:", offered1)

    assert any("Range Decomposition for Salary Estimate" in t for t in offered1), (
        f"expected the salary range-decomposition candidate offered, got {offered1}"
    )
    assert any("Job Title Categorization" in t for t in offered1), (
        f"expected the job-title-categorization candidate offered, got {offered1}"
    )
    assert any("Categorical Consolidation for Job Title" in t for t in offered1), (
        f"expected the Spec 3 categorical_consolidation candidate for Job Title offered, got {offered1}"
    )
    assert not any("Company Age" in t for t in offered1), (
        f"company-age is irrelevant to this question and must NOT be offered, got {offered1}"
    )
    assert not any("Skill Keywords" in t for t in offered1), (
        f"skill-keywords is irrelevant to this question and must NOT be offered, got {offered1}"
    )
    assert not any("Same State Flag" in t for t in offered1), (
        f"same-state-flag is irrelevant to this question and must NOT be offered, got {offered1}"
    )
    assert set(result1.keys()) == {"transformation_narrative_log", "transformation_candidates_not_relevant"}, (
        f"nothing was applied (everything skipped), so only the Spec 2 narrative-log "
        f"fields should be returned — no prompt_query_context refresh, got {result1.keys()}"
    )
    assert len(result1["transformation_narrative_log"]) == 3, result1["transformation_narrative_log"]
    assert all(e["chosen_option_id"] == "skip" and e["fresh"] is True for e in result1["transformation_narrative_log"])
    print("PASS: exactly the relevant candidates (salary, job title x2 including Spec 3's "
          "categorical_consolidation) were offered; "
          "irrelevant ones (company age, skill keywords, same-state) were not; "
          "nothing was applied since everything was skipped (both real 'skip' decisions "
          "were still recorded to transformation_narrative_log for Spec 2 narration).\n")

    print("=" * 70)
    print("TEST 2: a company-age question surfaces company-age (new), while")
    print("the already-decided salary/job-title candidates are reused silently")
    print("=" * 70)

    state2 = SQLAnalystState(
        user_question="How does company age relate to average rating?",
        curated_question="How does company age relate to average rating?",
    )
    ctx2 = add_context(state2)
    state2_with_ctx = state2.model_copy(update=ctx2)

    captured2 = StringIO()
    with patch("sys.stdin.isatty", return_value=True):
        original_stdout = sys.stdout
        sys.stdout = captured2
        try:
            result2 = surface_transformations(state2_with_ctx)
        finally:
            sys.stdout = original_stdout
    printed2 = captured2.getvalue()
    offered2 = _offered_titles(printed2)
    print("offered:", offered2)

    assert any("Company Age" in t for t in offered2), (
        f"expected company-age offered now that the question is about it, got {offered2}"
    )
    assert not any("Range Decomposition for Salary Estimate" in t for t in offered2), (
        f"the salary candidate was already decided in Test 1 — must be reused silently, "
        f"not re-asked, got {offered2}"
    )
    assert not any("Job Title Categorization" in t for t in offered2), (
        f"the job-title candidate was already decided in Test 1 — must be reused silently, "
        f"not re-asked, got {offered2}"
    )
    assert not any("Categorical Consolidation for Job Title" in t for t in offered2), (
        f"the Spec 3 categorical_consolidation candidate was already decided in Test 1 — "
        f"must be reused silently, not re-asked, got {offered2}"
    )
    assert set(result2.keys()) == {"transformation_narrative_log", "transformation_candidates_not_relevant"}, (
        f"nothing was applied (everything skipped), so only the Spec 2 narrative-log "
        f"fields should be returned, got {result2.keys()}"
    )
    assert len(result2["transformation_narrative_log"]) == 1, result2["transformation_narrative_log"]
    assert result2["transformation_narrative_log"][0]["candidate"]["kind"] == "company_age"
    assert result2["transformation_narrative_log"][0]["chosen_option_id"] == "skip"
    assert result2["transformation_narrative_log"][0]["fresh"] is True
    print("PASS: company-age is offered for the first time; salary/job-title decisions from "
          "Test 1 are reused silently, never re-asked.\n")

finally:
    invalidate_cached_decisions_for_table(conn, TABLE)
    write_transformation_candidates(conn, TABLE, original_candidates)  # restore real state
    conn.close()

print("=" * 70)
print("ALL LIVE-WIRING (SPEC 1 ACCEPTANCE TESTS 1-2) ASSERTIONS PASSED")
print("=" * 70)
