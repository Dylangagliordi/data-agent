"""
Regression test for architecture review point #25: the minimum-sample-size
rule (HAVING COUNT(*) >= 5 for a per-category ranking by an averaged/rate
metric) was previously only DISCLOSED if present in the generated SQL —
nothing ever caught the rule being silently skipped entirely, since
disclosure only extracts what's actually there.

Test 1 (unit, no LLM): _min_sample_rule_violated fires only for the intended
shape (ranking question + GROUP BY + averaged/rate metric + no HAVING
threshold + not "regardless of size"), and stays silent for every case that
should NOT trigger it.

Test 2 (live generate_sql, monkeypatched LLM): forces the first LLM call to
return a genuinely non-compliant query (GROUP BY + AVG + no HAVING) for a
ranking question, and confirms generate_sql catches this and triggers a
real second (regeneration) call whose corrected output is what generate_sql
actually returns — proving the corrective loop fires rather than silently
executing the non-compliant query. Follows the same monkeypatch-pick_llm
pattern used in tests/test_is_safe.py.
"""

import agents.sql_analyst as sql_analyst_module
from agents.sql_analyst import (
    _extract_min_sample_threshold,
    _min_sample_rule_violated,
    generate_sql,
)
from models.schema import SQLAnalystState

print("=" * 70)
print("TEST 1: _min_sample_rule_violated — unit cases")
print("=" * 70)

NON_COMPLIANT_SQL = (
    "SELECT industry, AVG(salary) AS avg_salary FROM jobs GROUP BY industry "
    "ORDER BY avg_salary DESC;"
)
COMPLIANT_SQL = (
    "SELECT industry, AVG(salary) AS avg_salary FROM jobs GROUP BY industry "
    "HAVING COUNT(*) >= 5 ORDER BY avg_salary DESC;"
)
RANKING_QUESTION = "Which industry has the highest average salary?"
NON_RANKING_QUESTION = "What is the average salary per industry?"
REGARDLESS_QUESTION = "Which industry has the highest average salary, regardless of size?"
NO_GROUP_BY_SQL = "SELECT AVG(salary) FROM jobs;"
TOTAL_NOT_AVG_SQL = "SELECT industry, SUM(salary) AS total FROM jobs GROUP BY industry ORDER BY total DESC;"

cases = [
    ("ranking question + non-compliant SQL -> VIOLATED", RANKING_QUESTION, NON_COMPLIANT_SQL, True),
    ("ranking question + compliant SQL -> ok", RANKING_QUESTION, COMPLIANT_SQL, False),
    ("non-ranking question + non-compliant SQL -> ok (rule doesn't apply)", NON_RANKING_QUESTION, NON_COMPLIANT_SQL, False),
    ("'regardless of size' question -> ok (explicitly opted out)", REGARDLESS_QUESTION, NON_COMPLIANT_SQL, False),
    ("no GROUP BY at all -> ok (not a per-category ranking)", RANKING_QUESTION, NO_GROUP_BY_SQL, False),
    ("GROUP BY + SUM (not averaged/rate) -> ok (rule targets avg/rate only)", RANKING_QUESTION, TOTAL_NOT_AVG_SQL, False),
]
for label, question, sql, expected in cases:
    result = _min_sample_rule_violated(question, sql)
    assert result == expected, f"{label}: expected {expected}, got {result}"
    print(f"PASS: {label}")

print()

print("=" * 70)
print("TEST 2: generate_sql catches a non-compliant first attempt and")
print("regenerates once with the missing rule made explicit")
print("=" * 70)


class _FakeResponse:
    def __init__(self, content):
        self.content = content


class _FakeLLM:
    """Returns NON_COMPLIANT_SQL on its first .invoke() call, then
    COMPLIANT_SQL on every subsequent call — simulating an LLM that skipped
    the required HAVING clause on its first attempt."""

    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        if self.call_count == 1:
            return _FakeResponse(NON_COMPLIANT_SQL)
        return _FakeResponse(COMPLIANT_SQL)


fake_llm = _FakeLLM()
original_pick_llm = sql_analyst_module.pick_llm
sql_analyst_module.pick_llm = lambda level: fake_llm

try:
    state = SQLAnalystState(
        curated_question=RANKING_QUESTION,
        prompt_query_context="Table: jobs\nColumns:\n  - industry (text)\n  - salary (numeric)",
    )
    result = generate_sql(state)
    final_sql = result["generated_sql_query"]
    print(f"LLM call count: {fake_llm.call_count}")
    print(f"final generated_sql_query: {final_sql}")

    assert fake_llm.call_count == 2, (
        f"expected exactly 2 LLM calls (initial + one corrective regeneration), "
        f"got {fake_llm.call_count}"
    )
    threshold = _extract_min_sample_threshold(final_sql)
    assert threshold is not None and int(threshold) == 5, (
        f"generate_sql must return the CORRECTED (compliant) query, not the "
        f"non-compliant first attempt: {final_sql!r}"
    )
    assert not _min_sample_rule_violated(RANKING_QUESTION, final_sql), (
        "final query must no longer violate the minimum-sample-size rule"
    )
    print("PASS: generate_sql detected the missing HAVING clause and returned "
          "the regenerated, compliant query instead of silently executing the "
          "non-compliant one.\n")
finally:
    sql_analyst_module.pick_llm = original_pick_llm


print("=" * 70)
print("TEST 3: a compliant first attempt makes only ONE LLM call (no wasted retry)")
print("=" * 70)


class _AlwaysCompliantLLM:
    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        return _FakeResponse(COMPLIANT_SQL)


compliant_llm = _AlwaysCompliantLLM()
sql_analyst_module.pick_llm = lambda level: compliant_llm
try:
    state2 = SQLAnalystState(
        curated_question=RANKING_QUESTION,
        prompt_query_context="Table: jobs\nColumns:\n  - industry (text)\n  - salary (numeric)",
    )
    result2 = generate_sql(state2)
    assert compliant_llm.call_count == 1, (
        f"a compliant first attempt must NOT trigger a second LLM call, "
        f"got {compliant_llm.call_count} calls"
    )
    print("PASS: compliant first attempt made exactly 1 LLM call.\n")
finally:
    sql_analyst_module.pick_llm = original_pick_llm

print("=" * 70)
print("ALL MIN-SAMPLE-RULE-COMPLIANCE ASSERTIONS PASSED")
print("=" * 70)
