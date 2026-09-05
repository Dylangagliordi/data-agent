"""
Unit tests (no live LLM, no DB) for the LLM-driven exploratory discovery phase in
utils/data_cleaning.py: explore_column, _verify_hypothesis, _explore_column_pairs,
explore_and_verify.

check_rubric() is a closed catalog of 25 hardcoded pattern-matchers — it only catches
problem shapes someone anticipated in advance. These tests confirm the new discovery
phase catches what it structurally cannot: a real, human-obvious composite-field issue
(two variables glued together in one column) and a silently-duplicated column — while
never trusting an LLM's "notice" on its own word. Every test injects a deterministic
fake LLM via the llm= parameter (same injection pattern as test_auto_clean_redirect.py
/ test_min_sample_rule_compliance.py), so hypothesis proposal is fully controlled while
the actual VERIFICATION step (does the proposed pattern really hold up against the real
full column) runs as real, unmocked Python/pandas — that's the trust boundary under test.

Test 1: reproduces the real Uncleaned_DS_jobs.csv bug — a "Company Name" column whose
real values are the company name and its Glassdoor rating glued together with a literal
newline (e.g. "Healthfirst\\n3.1"). Confirms explore_column proposes a hypothesis,
_verify_hypothesis mechanically confirms it against the FULL column with a real match
fraction, and the resulting issue is tagged "(discovered)", fail-level, and appears in
explore_and_verify's final issue list.

Test 2: negative control — a column with no real composite structure produces zero
surviving hypotheses, both when the LLM proposes nothing at all, and (a stronger check)
when the LLM proposes something anyway but the mechanical full-column check rejects it.

Test 3: a fixture where one column ("Sector") is an exact copy of another ("Industry")
— confirms _explore_column_pairs catches it via real full-column comparison, not just
the LLM's suspicion.

Test 4: a table with more eligible columns than EXPLORE_MAX_LLM_CALLS_PER_TABLE allows
(monkeypatched down for this test) logs a warning to stderr and completes without
raising, having used only its allotted budget.
"""

import io
import sys
from contextlib import redirect_stderr

import pandas as pd

import utils.data_cleaning as dc
from models.schema import ExplorationHypothesis, VerifiedPatternProposal


class DispatchLLM:
    """Fake chat model: with_structured_output(schema_cls) followed by invoke(messages)
    returns a canned response looked up by (schema type, a substring of the human
    message content). Falls back to a real, inert "nothing found" response for the
    requested schema when nothing matches, mirroring what a real LLM finding nothing
    would return — never raises, so untested columns/pairs are silently a no-op.
    """

    def __init__(self, hypothesis_responses=None, verify_responses=None):
        # Each is a list of (substring_to_match_in_human_content, response_object).
        self.hypothesis_responses = hypothesis_responses or []
        self.verify_responses = verify_responses or []
        self.call_count = 0
        self._schema_cls = None

    def with_structured_output(self, schema_cls):
        self._schema_cls = schema_cls
        return self

    def invoke(self, messages):
        self.call_count += 1
        human_text = messages[-1][1]
        if self._schema_cls is ExplorationHypothesis:
            for substr, resp in self.hypothesis_responses:
                if substr in human_text:
                    return resp
            return ExplorationHypothesis(hypotheses=[])
        if self._schema_cls is VerifiedPatternProposal:
            for substr, resp in self.verify_responses:
                if substr in human_text:
                    return resp
            # A pattern that can never match anything real, at an unreachable
            # threshold — mechanical verification will always reject this.
            return VerifiedPatternProposal(pattern="(?!)", match_threshold=1.1, description="no match")
        raise AssertionError(f"unexpected schema requested: {self._schema_cls}")


print("=" * 70)
print("TEST 1: composite-field discovery — reproduces the real")
print("Uncleaned_DS_jobs.csv 'Company Name' bug (name glued to rating via \\n)")
print("=" * 70)

N = 30
company_composite_values = [f"Company{i}\n{2.5 + (i % 20) * 0.1:.1f}" for i in range(N)]
df1 = pd.DataFrame({
    "Company Name": company_composite_values,
    "Job Title": [f"Data Scientist {i}" for i in range(N)],
})

composite_hypothesis = ExplorationHypothesis(hypotheses=[
    "This column may hold two variables joined by a newline, with the second part "
    "looking like a decimal rating number."
])
composite_proposal = VerifiedPatternProposal(
    pattern=r"\n\d\.\d$",
    match_threshold=0.8,
    description="company name glued to a decimal rating via a newline",
)
fake_llm1 = DispatchLLM(
    hypothesis_responses=[("Column name: Company Name", composite_hypothesis)],
    verify_responses=[("Column name: Company Name", composite_proposal)],
)

# 1a. explore_column proposes the hypothesis (loose text, not an issue string).
hyps = dc.explore_column(df1, "Company Name", llm=fake_llm1)
print("explore_column hypotheses:", hyps)
assert hyps == composite_hypothesis.hypotheses, f"expected the proposed hypothesis, got {hyps}"
assert not hyps[0].startswith("Composite field"), "explore_column must return LOOSE text, never a formatted issue"
print("PASS: explore_column returns a loose hypothesis, not a check_rubric-shaped issue.\n")

# 1b. _verify_hypothesis mechanically confirms it against the FULL column, and
# returns the verified pattern record alongside the issue string (threaded through
# to fix-generation by a later task — see test_composite_field_split.py).
verified, verified_pattern = dc._verify_hypothesis(df1, "Company Name", hyps[0], llm=fake_llm1)
print("verified issue:", verified)
print("verified pattern:", verified_pattern)
assert verified is not None, "hypothesis must be confirmed — the real column genuinely matches"
assert verified.startswith("Composite field (discovered):"), f"wrong tag: {verified!r}"
assert f"{N} value(s)" in verified, f"expected all {N} rows to match, got: {verified!r}"
assert "100%" in verified, f"expected 100% match, got: {verified!r}"
assert dc._issue_severity(verified) == "fail", "a discovered composite field must be fail-level"
assert verified_pattern == {"issue": verified, "pattern": r"\n\d\.\d$", "match_threshold": 1.0}, (
    f"expected the real verified pattern record, got {verified_pattern}"
)
print("PASS: _verify_hypothesis mechanically confirmed the pattern against the real full "
      "column, tagged the result '(discovered)', classified fail-level, and returned the "
      "real verified pattern record.\n")

# 1c. Appears in explore_and_verify's final issue list (the "final issue list" the
# deliverable spec asks for), and Job Title (no real composite structure, and this
# fake LLM proposes nothing for it) contributes nothing. pattern_lookup carries the
# same verified pattern record, keyed by the issue string.
issues1, pattern_lookup1 = dc.explore_and_verify(df1, llm=fake_llm1, flagged_columns=set())
print("explore_and_verify issues:", issues1)
print("explore_and_verify pattern_lookup:", pattern_lookup1)
composite_issues = [i for i in issues1 if i.startswith("Composite field (discovered):")]
assert len(composite_issues) == 1, f"expected exactly 1 discovered composite issue, got {issues1}"
assert "Company Name" in composite_issues[0]
assert pattern_lookup1.get(composite_issues[0]) == verified_pattern, (
    f"pattern_lookup must map the issue string to its verified pattern record, got {pattern_lookup1}"
)
print("PASS: the discovered issue appears in explore_and_verify's final issue list, with its "
      "verified pattern record available via pattern_lookup.\n")


print("=" * 70)
print("TEST 2: negative control — a column with no real composite structure")
print("produces zero surviving hypotheses")
print("=" * 70)

cities = (["Springfield", "Shelbyville", "Ogdenville", "North Haverbrook"] * 8)[:25]
df2 = pd.DataFrame({"City": cities})

# 2a. The LLM notices nothing at all (the common, expected case for a genuinely
# unremarkable column) — explore_column returns [].
no_op_llm = DispatchLLM()
hyps2 = dc.explore_column(df2, "City", llm=no_op_llm)
assert hyps2 == [], f"expected no hypotheses for a genuinely unremarkable column, got {hyps2}"
print("PASS: explore_column proposes nothing for a column with no real structure.\n")

# 2b. Stronger check: the LLM proposes a hypothesis anyway (LLMs aren't perfect
# noticers), but the mechanical full-column check rejects it because the pattern
# doesn't actually hold up against the real data — this is the trust boundary,
# not the noticing step, doing the real work.
bogus_hypothesis = ExplorationHypothesis(hypotheses=[
    "This column might encode a hidden numeric suffix after each city name."
])
bogus_proposal = VerifiedPatternProposal(
    pattern=r"\d+$", match_threshold=0.5, description="hidden numeric suffix",
)
fake_llm2 = DispatchLLM(
    hypothesis_responses=[("Column name: City", bogus_hypothesis)],
    verify_responses=[("Column name: City", bogus_proposal)],
)
verified2, verified_pattern2 = dc._verify_hypothesis(df2, "City", bogus_hypothesis.hypotheses[0], llm=fake_llm2)
assert verified2 is None, f"a hypothesis that doesn't hold up must be discarded, got {verified2!r}"
assert verified_pattern2 is None, f"pattern record must also be None when discarded, got {verified_pattern2!r}"
print("PASS: a proposed-but-false hypothesis is mechanically rejected (0% real match "
      "against a 50% threshold).\n")

issues2, pattern_lookup2 = dc.explore_and_verify(df2, llm=fake_llm2, flagged_columns=set())
assert issues2 == [], f"expected zero surviving issues for a genuinely clean column, got {issues2}"
assert pattern_lookup2 == {}, f"expected an empty pattern_lookup, got {pattern_lookup2}"
print("PASS: explore_and_verify's final issue list (and pattern_lookup) are empty for a "
      "genuinely clean column.\n")


print("=" * 70)
print("TEST 3: _explore_column_pairs catches an exact-copy column")
print("=" * 70)

M = 25
df3 = pd.DataFrame({
    "Industry": [f"Industry{i % 5}" for i in range(M)],
    "Sector": [f"Industry{i % 5}" for i in range(M)],  # exact copy of Industry
    "Revenue": [f"${(i + 1) * 1000}" for i in range(M)],
})
pair_suspicion = ExplorationHypothesis(hypotheses=[
    "These two columns look identical — Sector may have been silently overwritten "
    "by a transform of Industry."
])
fake_llm3 = DispatchLLM(
    hypothesis_responses=[("Column A: Industry", pair_suspicion)],
)

issues3 = dc._explore_column_pairs(df3, llm=fake_llm3)
print("column-pair issues:", issues3)
dup_issues = [i for i in issues3 if i.startswith("Duplicate column (discovered):")]
assert len(dup_issues) == 1, f"expected exactly 1 discovered duplicate-column issue, got {issues3}"
assert "Industry" in dup_issues[0] and "Sector" in dup_issues[0], dup_issues[0]
assert "100%" in dup_issues[0], f"expected 100% overlap, got: {dup_issues[0]!r}"
assert dc._issue_severity(dup_issues[0]) == "fail", "a discovered duplicate column must be fail-level"
print("PASS: _explore_column_pairs mechanically confirmed the exact-copy column pair "
      "and tagged the result '(discovered)', classified fail-level.\n")

# Sanity: Revenue (genuinely distinct from both) never appears in any finding.
assert not any("Revenue" in i for i in issues3), f"Revenue must not be flagged: {issues3}"
print("PASS: the genuinely distinct 'Revenue' column was not flagged.\n")


print("=" * 70)
print("TEST 4: exceeding EXPLORE_MAX_LLM_CALLS_PER_TABLE logs a warning and")
print("completes without raising")
print("=" * 70)

K = 25
df4 = pd.DataFrame({
    f"col_{i}": [f"value_{i}_{j}" for j in range(K)] for i in range(5)
})
fake_llm4 = DispatchLLM()  # always returns empty hypotheses — cheap, always "succeeds"

original_ceiling = dc.EXPLORE_MAX_LLM_CALLS_PER_TABLE
dc.EXPLORE_MAX_LLM_CALLS_PER_TABLE = 2
try:
    stderr_capture = io.StringIO()
    with redirect_stderr(stderr_capture):
        issues4, pattern_lookup4 = dc.explore_and_verify(df4, llm=fake_llm4, flagged_columns=set())
    stderr_text = stderr_capture.getvalue()
    print("stderr:", stderr_text.strip())
    print("issues4:", issues4)
    print("fake_llm4.call_count:", fake_llm4.call_count)

    assert issues4 == [], f"expected no issues (fake LLM never proposes anything), got {issues4}"
    assert "[explore] LLM call ceiling" in stderr_text, (
        f"expected a ceiling-reached warning on stderr, got: {stderr_text!r}"
    )
    assert fake_llm4.call_count <= 2, (
        f"expected discovery to stop at the ceiling (2 calls), got {fake_llm4.call_count}"
    )
finally:
    dc.EXPLORE_MAX_LLM_CALLS_PER_TABLE = original_ceiling

print("PASS: hitting the LLM call ceiling logs a warning and returns cleanly, "
      "never raising.\n")

print("=" * 70)
print("ALL DATA-CLEANING DISCOVERY-PHASE ASSERTIONS PASSED")
print("=" * 70)
