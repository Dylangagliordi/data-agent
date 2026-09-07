"""
Spec 2 (Final): tests utils/narrative.py — the single shared, ordered
narrative walkthrough both generate_report.py and generate_presentation.py
render from.

Requires a live Postgres (same as tests/test_transformation_options.py) —
uses the real, already-logged cleaning history for the real uncleaned_ds_jobs
table. No LLM/network needed for build_narrative_walkthrough itself (pure
assembly of real facts); narrate_steps is tested separately with a fake LLM.

Run as:
    PYTHONPATH=. uv run python tests/test_narrative_walkthrough.py
"""

import json

from utils.narrative import NarrativeStep, build_narrative_walkthrough, narrate_steps

print("=" * 70)
print("TEST 1: build_narrative_walkthrough against real cleaning history")
print("(uncleaned_ds_jobs) — structure, ordering, honesty")
print("=" * 70)

entry = {
    "generated_sql_query": "SELECT job_title, avg(rating) AS avg_rating FROM uncleaned_ds_jobs GROUP BY job_title HAVING COUNT(*) >= 5 ORDER BY avg_rating DESC LIMIT 5",
    "sql_query_execution_result": json.dumps(
        {"columns": ["job_title", "avg_rating"], "rows": [["Data Scientist", 4.2]], "truncated": False}
    ),
    "final_answer": "Data Scientist roles have the highest average rating at 4.2.",
    "chart_type": "",
    "transformation_narrative_log": [
        {
            "table_name": "uncleaned_ds_jobs",
            "candidate": {
                "candidate_id": "abc123",
                "kind": "range_decomposition",
                "columns": ["Salary Estimate"],
                "description": "Column 'Salary Estimate' has 100% of its real values matching a numeric range pattern.",
                "relevance_tags": ["salary", "compensation"],
            },
            "chosen_option_id": "apply",
            "reasoning_shown": {
                "context": {
                    "title": "Range Decomposition for Salary Estimate?",
                    "what_was_found": "100% of real values match a numeric range pattern.",
                    "why_optional": "This is enrichment, not a correctness fix.",
                },
                "options": [
                    {"id": "apply", "label": "Apply now", "description": "Add min/max/avg columns."},
                    {"id": "skip", "label": "Skip / do nothing", "description": "Leave the data as-is."},
                ],
            },
            "fresh": True,
            "reload_reask": False,
            "decided_at": None,
        },
        {
            "table_name": "uncleaned_ds_jobs",
            "candidate": {
                "candidate_id": "def456",
                "kind": "company_age",
                "columns": ["Founded"],
                "description": "Founded year can be turned into a company_age column.",
                "relevance_tags": ["company age", "founding", "tenure"],
            },
            "chosen_option_id": "apply_null",
            "reasoning_shown": {
                "context": {"title": "Company Age for Founded?", "what_was_found": "f", "why_optional": "w"},
                "options": [
                    {"id": "apply_null", "label": "Apply (missing founding year -> null age)", "description": "d"},
                    {"id": "skip", "label": "Skip / do nothing", "description": "Leave the data as-is."},
                ],
            },
            "fresh": False,
            "reload_reask": False,
            "decided_at": "2026-09-05T12:00:00+00:00",
        },
    ],
    "transformation_candidates_not_relevant": [
        {
            "table_name": "uncleaned_ds_jobs",
            "candidate": {
                "candidate_id": "ghi789",
                "kind": "skill_keywords",
                "columns": ["Job Description"],
                "description": "Job Description can be scanned for skill keywords.",
                "relevance_tags": ["skill", "technology", "tools"],
            },
        }
    ],
}

steps = build_narrative_walkthrough(entry)
assert steps, "expected at least one narrative step"
assert all(isinstance(s, NarrativeStep) for s in steps)

# Step numbers are sequential starting at 1.
assert [s.step_number for s in steps] == list(range(1, len(steps) + 1)), (
    "step_number must be sequential"
)

# Part ordering: cleaning steps, then transformation steps, then analysis steps
# — never interleaved.
parts_seen_order = []
for s in steps:
    if not parts_seen_order or parts_seen_order[-1] != s.part:
        parts_seen_order.append(s.part)
assert parts_seen_order == ["cleaning", "transformation", "analysis"], (
    f"expected strict cleaning -> transformation -> analysis part ordering, got {parts_seen_order}"
)
print(f"PASS: {len(steps)} steps, sequential numbering, correct part ordering ({parts_seen_order}).")

cleaning_steps = [s for s in steps if s.part == "cleaning"]
transformation_steps = [s for s in steps if s.part == "transformation"]
analysis_steps = [s for s in steps if s.part == "analysis"]

# Load step present with real row/column counts.
load_steps = [s for s in cleaning_steps if s.title.startswith("Load ")]
assert load_steps, "expected a Load step"
assert load_steps[0].stats.get("row_count", 0) > 0
print(f"PASS: Load step present with real row_count={load_steps[0].stats['row_count']}.")

# A composite-field split step exists, honestly narrated (match %, honest
# discovery-coverage caveat), and never claims a fabricated pair of column
# names without evidence.
composite_steps = [s for s in cleaning_steps if "Split" in s.title]
assert composite_steps, "expected at least one composite-field split step"
for cs in composite_steps:
    assert "%" in cs.explanation, f"composite step should state a real match percentage: {cs.explanation}"
    assert "exploratory discovery pass" in cs.explanation, (
        "composite step must include the honest 'not blanket discovery' caveat"
    )
print(f"PASS: {len(composite_steps)} composite-field split step(s), each with a real match % and the honest discovery caveat.")

# A batch step exists (the real placeholder batch for uncleaned_ds_jobs),
# names every column it covered, and states the real shared reason.
batch_steps = [s for s in cleaning_steps if s.stats.get("batched")]
assert batch_steps, "expected at least one batched-fix step"
for bs in batch_steps:
    assert bs.stats.get("columns"), f"batch step must name its covered columns: {bs.stats}"
    assert "same" in bs.explanation or "identical" in bs.explanation
print(f"PASS: {len(batch_steps)} batch step(s), each naming its real covered columns.")

# Duplicate-row step is always present (even if none found) — never silently
# omitted.
dup_steps = [s for s in cleaning_steps if "Duplicate-row check" in s.title]
assert dup_steps, "expected a duplicate-row check step (even a 'none found' one)"
print(f"PASS: duplicate-row check step present: {dup_steps[0].explanation!r}")

# Part B: exactly the two transformation decisions supplied, fresh vs reused
# narrated differently, no fabricated reasoning.
assert len(transformation_steps) == 2 + 1, (  # 2 decisions + 1 "not relevant" note
    f"expected 2 decision steps + 1 not-relevant note, got {len(transformation_steps)}"
)
fresh_step = next(s for s in transformation_steps if s.stats.get("fresh") is True)
reused_step = next(s for s in transformation_steps if s.stats.get("fresh") is False and not s.stats.get("low_emphasis"))
assert "for the first time in this run" in fresh_step.explanation
assert "already been decided earlier" in reused_step.explanation
assert "2026-09-05T12:00:00" in reused_step.explanation
assert "No additional reason beyond this choice was recorded" in fresh_step.explanation
assert "No additional reason beyond this choice was recorded" in reused_step.explanation
print("PASS: fresh decision narrated as 'decided for the first time'; reused decision narrated with its real decided_at timestamp; neither step fabricates a reason.")

not_relevant_step = next(s for s in transformation_steps if s.stats.get("low_emphasis"))
assert "skill keywords for Job Description" in not_relevant_step.explanation
assert "weren't relevant to this particular question" in not_relevant_step.explanation
print("PASS: 'other available transformations' note lists the real unsurfaced candidate.")

# Part C: question shaping + final result present, grounded in real values.
shaping_step = next(s for s in analysis_steps if s.title == "Shape the data for this question")
assert "HAVING" not in shaping_step.explanation  # prose, not raw SQL keywords
assert "grouped the data by" in shaping_step.explanation.lower() or "job_title" in shaping_step.explanation
assert shaping_step.stats.get("having_threshold") == 5
print("PASS: question-shaping step reflects the real GROUP BY / HAVING threshold.")

final_step = next(s for s in analysis_steps if s.title == "The final result")
assert final_step.explanation == entry["final_answer"], "final result step must quote the real final_answer verbatim"
assert final_step.stats.get("columns") == ["job_title", "avg_rating"]
print("PASS: final-result step quotes the real final_answer and carries the real result columns.")

# No chart_type in this entry -> no chart step.
assert not any("Visualize the result" in s.title for s in steps)
print("PASS: no chart step when chart_type is absent.\n")


print("=" * 70)
print("TEST 2: a dataset with no composite splits, no batching, and zero")
print("surfaced Part B candidates still produces a coherent Part A + Part C")
print("narrative, with Part B omitted entirely (Spec 2 acceptance test 7)")
print("=" * 70)

minimal_entry = {
    "generated_sql_query": "SELECT COUNT(*) AS n FROM uncleaned_ds_jobs",
    "sql_query_execution_result": json.dumps({"columns": ["n"], "rows": [[672]], "truncated": False}),
    "final_answer": "There are 672 rows.",
    "chart_type": "",
    "transformation_narrative_log": [],
    "transformation_candidates_not_relevant": [],
}
minimal_steps = build_narrative_walkthrough(minimal_entry)
assert any(s.part == "cleaning" for s in minimal_steps)
assert not any(s.part == "transformation" for s in minimal_steps), "Part B must be omitted entirely when nothing was surfaced"
assert any(s.part == "analysis" for s in minimal_steps)
print(f"PASS: {len(minimal_steps)} steps, Part B cleanly omitted, Part A + Part C still present.\n")


print("=" * 70)
print("TEST 3: narrate_steps — validates length, falls back on mismatch/failure")
print("=" * 70)


class _GoodStructured:
    def __init__(self, n):
        self.explanations = [f"Narrated step {i}." for i in range(n)]


class _GoodLLM:
    def __init__(self, n):
        self.n = n

    def with_structured_output(self, schema_cls):
        return self

    def invoke(self, messages):
        return _GoodStructured(self.n)


class _MismatchedStructured:
    explanations = ["only one explanation"]


class _MismatchedLLM:
    def with_structured_output(self, schema_cls):
        return self

    def invoke(self, messages):
        return _MismatchedStructured()


class _RaisingLLM:
    def with_structured_output(self, schema_cls):
        return self

    def invoke(self, messages):
        raise RuntimeError("simulated LLM failure")


narrated_ok = narrate_steps(steps, _GoodLLM(len(steps)))
assert len(narrated_ok) == len(steps)
assert all(n.explanation == f"Narrated step {i}." for i, n in enumerate(narrated_ok))
assert [n.step_number for n in narrated_ok] == [s.step_number for s in steps]
assert [n.part for n in narrated_ok] == [s.part for s in steps]
print("PASS: a correctly-shaped structured response replaces every step's explanation, preserving order/part/count.")

narrated_mismatch = narrate_steps(steps, _MismatchedLLM())
assert [n.explanation for n in narrated_mismatch] == [s.explanation for s in steps], (
    "a length mismatch must fall back to the original deterministic explanations"
)
print("PASS: a length-mismatched response falls back to the deterministic explanations, never drops a step.")

narrated_failure = narrate_steps(steps, _RaisingLLM())
assert [n.explanation for n in narrated_failure] == [s.explanation for s in steps]
print("PASS: an LLM failure falls back to the deterministic explanations.")

assert narrate_steps([], _GoodLLM(0)) == []
print("PASS: an empty step list is a no-op.\n")

print("=" * 70)
print("TEST 4: a decision with genuinely empty/absent reasoning_shown states")
print("only the choice, never a fabricated reason (Spec 2's explicit rule)")
print("=" * 70)

from utils.narrative import _add_transformation_step

for label, reasoning_shown_value in [("empty dict", {}), ("None", None)]:
    empty_steps: list = []
    empty_counter = {"n": 0}

    def _empty_add(part, title, explanation, technical_detail="", stats=None):
        empty_counter["n"] += 1
        empty_steps.append(NarrativeStep(empty_counter["n"], part, title, explanation, technical_detail, stats or {}))

    _add_transformation_step(_empty_add, {
        "table_name": "uncleaned_ds_jobs",
        "candidate": {"candidate_id": "x", "kind": "categorical_consolidation", "columns": ["Industry"],
                      "description": "Column 'Industry' has 57 distinct real values across 672 rows.",
                      "relevance_tags": []},
        "chosen_option_id": "apply", "reasoning_shown": reasoning_shown_value,
        "fresh": True, "reload_reask": False, "decided_at": None,
    })
    step = empty_steps[0]
    assert "No additional reason beyond this choice was recorded in the decision log." in step.explanation, (
        f"reasoning_shown={label!r}: deterministic explanation must explicitly state no reason was recorded"
    )
    assert "I chose: apply." in step.explanation
    for word in ("because", "in order to", "since this", "so that"):
        assert word not in step.explanation.lower(), (
            f"reasoning_shown={label!r}: deterministic explanation must never invent a rationale, "
            f"found {word!r} in {step.explanation!r}"
        )
    assert not step.stats.get("manual_mode"), f"reasoning_shown={label!r} must not be treated as manual mode"

print("PASS: for both reasoning_shown={} and reasoning_shown=None, the deterministic explanation "
      "states only the choice and explicitly discloses no reason was recorded — never a fabricated "
      "rationale. (The LLM narration pass's adherence to this same rule was verified empirically "
      "against live pick_llm output as part of this check — see the Spec 3 follow-up report; not "
      "re-run here as a scripted assertion, per this suite's existing convention of live-LLM checks "
      "being manual smoke tests, not baked into the deterministic regression run.)\n")

print("=" * 70)
print("ALL NARRATIVE-WALKTHROUGH (SPEC 2) ASSERTIONS PASSED")
print("=" * 70)
