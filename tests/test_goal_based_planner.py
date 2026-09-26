"""
Tests for Spec 14: Goal-Based Transformation Planner
(utils/transformation_options.py:plan_transformations_for_goal,
_rank_candidates_for_goal).

Uses the same real uncleaned_ds_jobs table and seeding discipline as
tests/test_transformation_options_live_wiring.py — real, stored candidates,
real Postgres decision cache, piped 'skip' answers. A fake structured-output
LLM proves the relevance-filtering logic deterministically (including that an
invented candidate id is dropped, never trusted); one real live call proves
the whole thing works end to end with a genuine model.

Run with:
    printf 'skip\\n%.0s' {1..20} | PYTHONPATH=. uv run python tests/test_goal_based_planner.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch

from models.schema import GoalRelevanceSchema
from utils.data_cleaning import _read_csv_robust
from utils.load_data import (
    get_admin_connection,
    invalidate_cached_decisions_for_table,
    read_transformation_decision,
    write_transformation_candidates,
)
from utils.transformation_options import (
    _rank_candidates_for_goal,
    detect_transformation_candidates,
    plan_transformations_for_goal,
)

TABLE = "uncleaned_ds_jobs"
RAW_PATH = "data/data-science-jobs/Uncleaned_DS_jobs.csv"


class _FakeStructuredLLM:
    def __init__(self, relevant_ids, reasoning="fake reasoning"):
        self.relevant_ids = relevant_ids
        self.reasoning = reasoning
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        return GoalRelevanceSchema(relevant_candidate_ids=self.relevant_ids, reasoning=self.reasoning)


class _FakeLLM:
    def __init__(self, structured):
        self.structured = structured

    def with_structured_output(self, schema_cls):
        return self.structured


def test_rank_candidates_for_goal_filters_invented_ids():
    candidates = detect_transformation_candidates(_read_csv_robust(RAW_PATH), TABLE)
    real_id = candidates[0].candidate_id
    fake_structured = _FakeStructuredLLM(relevant_ids=[real_id, "this_id_does_not_exist"])
    relevant = _rank_candidates_for_goal("some goal", candidates, _FakeLLM(fake_structured))
    assert relevant == {real_id}, "an invented candidate id must be silently dropped, never trusted"
    print("PASS: _rank_candidates_for_goal filters the LLM's response against real candidate ids only")


def test_plan_transformations_for_goal_real_wiring():
    conn = get_admin_connection()
    invalidate_cached_decisions_for_table(conn, TABLE)
    seed_df = _read_csv_robust(RAW_PATH)
    candidates = detect_transformation_candidates(seed_df, TABLE)
    write_transformation_candidates(conn, TABLE, candidates)

    salary_candidate = next(c for c in candidates if c.kind == "range_decomposition" and "Salary Estimate" in c.columns)
    job_title_cat_candidate = next(c for c in candidates if c.kind == "categorical_consolidation" and "Job Title" in c.columns)
    company_age_candidate = next(c for c in candidates if c.kind == "company_age")

    fake_structured = _FakeStructuredLLM(
        relevant_ids=[salary_candidate.candidate_id, job_title_cat_candidate.candidate_id]
    )
    fake_llm = _FakeLLM(fake_structured)

    try:
        with patch("sys.stdin.isatty", return_value=True):
            result = plan_transformations_for_goal(TABLE, "prepare this table for a salary prediction model", llm=fake_llm)

        assert result["table_name"] == TABLE
        decided_ids = {d["candidate_id"] for d in result["decided_this_run"]}
        assert salary_candidate.candidate_id in decided_ids
        assert job_title_cat_candidate.candidate_id in decided_ids
        assert company_age_candidate.candidate_id in result["not_relevant"], (
            "company_age was not in the fake LLM's relevant set, so it must be reported as not relevant, "
            "and never presented for a decision"
        )
        assert company_age_candidate.candidate_id not in decided_ids

        for d in result["decided_this_run"]:
            assert d["chosen_option_id"] in ("apply", "skip"), d
        print("PASS: plan_transformations_for_goal surfaces exactly the goal-relevant candidates and skips the rest")

        # Real cache proof: both decided candidates are now durably recorded,
        # the same _transformation_decisions cache a reactive question reads.
        assert read_transformation_decision(conn, TABLE, salary_candidate.candidate_id) is not None
        assert read_transformation_decision(conn, TABLE, job_title_cat_candidate.candidate_id) is not None
        assert read_transformation_decision(conn, TABLE, company_age_candidate.candidate_id) is None
        print("PASS: decisions made this way land in the real, shared _transformation_decisions cache")

        # A second call must not re-ask the two now-decided candidates.
        fake_structured_2 = _FakeStructuredLLM(relevant_ids=[company_age_candidate.candidate_id])
        with patch("sys.stdin.isatty", return_value=True):
            result2 = plan_transformations_for_goal(
                TABLE, "a second, different goal", llm=_FakeLLM(fake_structured_2)
            )
        already_decided_ids = set(result2["already_decided"])
        assert salary_candidate.candidate_id in already_decided_ids
        assert job_title_cat_candidate.candidate_id in already_decided_ids
        assert not any(d["candidate_id"] == salary_candidate.candidate_id for d in result2["decided_this_run"])
        print("PASS: a later call reuses already-decided candidates silently, never re-asking")
    finally:
        invalidate_cached_decisions_for_table(conn, TABLE)
        conn.close()


if __name__ == "__main__":
    test_rank_candidates_for_goal_filters_invented_ids()
    test_plan_transformations_for_goal_real_wiring()
    print("\nAll goal_based_planner tests passed.")
