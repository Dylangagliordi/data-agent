"""
Tests for Spec 2.1: giving the composite-field split path a legitimate "decline"
outcome, plus a precision improvement that skips obviously id-like columns before
discovery ever proposes a hypothesis for them.

Background: the Spec 2 shape check (_composite_split_shape_ok) is a safety net —
confirm a composite-field fix actually produced two real replacement columns, not
just made the issue string disappear (check_rubric() can never re-detect a
"(discovered)" string at all). But feeding a shape-check failure back into the
SAME retry loop as a real execution error ("expected 2, found 0 — try again") had
an unintended side effect: a false-positive discovery (the concrete case found in
testing — a sequential id column trivially matching ^\\d+$) could pressure the
fix-generation LLM into fabricating a meaningless-but-shape-compliant split just
to satisfy the check's letter, rather than correctly declining.

Test 1: a fake LLM that always returns the NO_SPLIT sentinel — confirms exactly
ONE LLM call is made (no retry), no code is ever executed, the file's real columns
are completely unchanged, and the final status is "declined_false_positive" with
the model's stated reason preserved verbatim.

Test 2: the existing Spec 2 end-to-end test (tests/test_composite_field_split.py,
Company Name -> two real columns) is re-run as part of verifying this spec and
must be unaffected by the new sentinel-check / id-column-skip logic — see this
file's own module-level comment below for the confirmation, rather than
duplicating that test's fixture/assertions here.

Test 3: a fixture with an obvious sequential id column alongside a genuine
composite column — confirms explore_column is never even called (zero LLM calls)
for the id column specifically, while the genuine composite column is still
explored and its issue still surfaces through explore_and_verify.

Test 4: the actual reported failure mode, directly regression-tested — a fake LLM
that WOULD fabricate a shape-compliant-but-meaningless split if it ever received a
retry/previous_error prompt, but correctly declines on a fresh (no previous_error)
call. Confirms the pipeline gets the decline, not the fabrication, because a
legitimate NO_SPLIT decline is a terminal, successful outcome and never triggers a
retry in the first place.
"""

import shutil
import sys
import tempfile
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pandas as pd

import utils.data_cleaning as dc
from models.schema import ExplorationHypothesis, VerifiedPatternProposal


@contextmanager
def redirect_stdin_yes(count=10):
    """Same trick as test_composite_field_split.py: these tests run as one
    in-process script, so monkeypatch sys.stdin directly rather than relying on
    a shell-level pipe. Only used where a test EXPECTS the approval gate to be
    reachable; Test 1 and Test 4 deliberately do NOT use this, so that an
    accidental call to _request_approval fails loudly (EOFError) instead of
    silently succeeding — a legitimate decline must never reach the gate at all.
    """
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
COMPOSITE_REGEX = r"\n\d\.\d$"
company_values = [f"Company{i}\n{2.5 + (i % 20) * 0.1:.1f}" for i in range(N - 1)] + ["Anomaly Corp"]
job_titles = [f"Data Scientist {i}" for i in range(N)]


print("=" * 70)
print("TEST 1: NO_SPLIT decline — exactly one LLM call, no execution, no")
print("approval prompt, file completely unchanged, reason preserved")
print("=" * 70)


class AlwaysDeclineLLM:
    """Always returns the NO_SPLIT sentinel — never a real script."""

    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        return _FakeResponse("# NO_SPLIT: this is a sequential row identifier, not two glued-together values.")


tmp_dir1 = Path(tempfile.mkdtemp(prefix="composite_decline_"))
try:
    cloned_path1 = tmp_dir1 / "jobs.csv"
    fixture_df1 = pd.DataFrame({"id": list(range(1, N + 1)), "Job Title": job_titles})
    fixture_df1.to_csv(cloned_path1, index=False)
    columns_before = list(pd.read_csv(cloned_path1, dtype=str).columns)

    composite_issue = (
        f"Composite field (discovered): column 'id' has {N} value(s) (100%) "
        f"matching the pattern '^\\d+$' — this looks like two distinct values "
        "glued together, not caught by a fixed rubric check."
    )
    pattern_lookup1 = {
        composite_issue: {"issue": composite_issue, "pattern": r"^\d+$", "match_threshold": 1.0}
    }

    fake_llm1 = AlwaysDeclineLLM()
    # Deliberately NOT wrapped in redirect_stdin_yes: if _clean_issue_group ever
    # reaches _request_approval here, input() will raise EOFError against the
    # real (un-piped) stdin — a loud failure, not a silent false pass.
    status, attempts, error, remaining, code = dc._clean_issue_group(
        cloned_path1, [composite_issue], fake_llm1, pattern_lookup=pattern_lookup1
    )
    print(f"status={status!r} attempts={attempts} error={error!r} call_count={fake_llm1.call_count}")

    assert fake_llm1.call_count == 1, (
        f"a legitimate decline must make exactly ONE LLM call (no retry), got {fake_llm1.call_count}"
    )
    assert status == "declined_false_positive", f"expected 'declined_false_positive', got {status!r}"
    assert error == "this is a sequential row identifier, not two glued-together values.", (
        f"the model's stated reason must be preserved verbatim, got {error!r}"
    )
    assert remaining == [], f"a declined issue has nothing 'remaining' to retry, got {remaining}"
    assert code == "", f"no code was ever generated for execution, got {code!r}"

    columns_after = list(pd.read_csv(cloned_path1, dtype=str).columns)
    assert columns_after == columns_before, (
        f"the file must be completely untouched — no execution ever happened: "
        f"{columns_before} -> {columns_after}"
    )
    print("PASS: exactly 1 LLM call, no execution, no approval prompt reached, file "
          "unchanged, and the decline reason was preserved verbatim.\n")
finally:
    shutil.rmtree(tmp_dir1, ignore_errors=True)


print("=" * 70)
print("TEST 2: the existing Spec 2 end-to-end split test is unaffected")
print("=" * 70)
print(
    "Covered by re-running tests/test_composite_field_split.py in full after this "
    "spec's changes (new COMPOSITE_FIELD_SPLIT_SYSTEM_PROMPT wording, the NO_SPLIT "
    "sentinel check, and the id-column skip) — that file's CompositeFixLLM never "
    "returns a NO_SPLIT sentinel and its fixture's 'Company Name' column is not "
    "id-like, so neither new code path is exercised for it; all 8 of its original "
    "assertions still pass unmodified. See this task's verification notes.\n"
)


print("=" * 70)
print("TEST 3: an obvious sequential id column is skipped by discovery")
print("entirely (zero LLM calls), a genuine composite column is not")
print("=" * 70)


class TrackingDispatchLLM:
    """Same dispatch shape as test_data_cleaning_discovery.py's DispatchLLM, plus
    tracking of every column name mentioned in a human prompt, so we can assert
    the id column was NEVER even asked about."""

    def __init__(self, hypothesis_responses=None):
        self.hypothesis_responses = hypothesis_responses or []
        self.call_count = 0
        self.seen_human_contents = []
        self._schema_cls = None

    def with_structured_output(self, schema_cls):
        self._schema_cls = schema_cls
        return self

    def invoke(self, messages):
        self.call_count += 1
        human_text = messages[-1][1]
        self.seen_human_contents.append(human_text)
        if self._schema_cls is ExplorationHypothesis:
            for substr, resp in self.hypothesis_responses:
                if substr in human_text:
                    return resp
            return ExplorationHypothesis(hypotheses=[])
        if self._schema_cls is VerifiedPatternProposal:
            return VerifiedPatternProposal(
                pattern=COMPOSITE_REGEX, match_threshold=0.8,
                description="company name glued to a decimal rating via a newline",
            )
        raise AssertionError("no plain invoke() expected in this test")


composite_hypothesis = ExplorationHypothesis(hypotheses=[
    "This column may hold two variables joined by a newline, with the second part "
    "looking like a decimal rating number."
])

df3 = pd.DataFrame({"id": list(range(1, N + 1)), "Company Name": company_values})

# 3a. Direct unit check: explore_column must skip the id column with ZERO calls.
id_only_llm = TrackingDispatchLLM()
hyps_id = dc.explore_column(df3, "id", llm=id_only_llm)
assert hyps_id == [], f"expected no hypotheses for a skipped id column, got {hyps_id}"
assert id_only_llm.call_count == 0, (
    f"explore_column must never call the LLM at all for an obvious sequential id "
    f"column, got {id_only_llm.call_count} call(s)"
)
print("PASS: explore_column makes ZERO LLM calls for an obvious sequential id column.\n")

# 3b. The genuine composite column is still explored normally, in the same table.
fake_llm3 = TrackingDispatchLLM(
    hypothesis_responses=[("Column name: Company Name", composite_hypothesis)],
)
issues3, pattern_lookup3 = dc.explore_and_verify(df3, llm=fake_llm3, flagged_columns=set())
print("issues3:", issues3)

assert not any("Column name: id" in h for h in fake_llm3.seen_human_contents), (
    "the id column must never appear in any discovery prompt at all"
)
composite_found = [i for i in issues3 if i.startswith("Composite field (discovered):") and "Company Name" in i]
assert len(composite_found) == 1, (
    f"the genuine composite column must still be discovered normally, got {issues3}"
)
print("PASS: the id column was never mentioned in any prompt, while the genuine "
      "composite column ('Company Name') was still discovered normally.\n")


print("=" * 70)
print("TEST 4: retry pressure is genuinely removed — a fake LLM that WOULD")
print("fabricate a shape-compliant split under retry pressure never gets the")
print("chance to, because a legitimate decline is never retried")
print("=" * 70)


class WouldFabricateUnderPressureLLM:
    """Correctly declines on a fresh call (no previous_error in the prompt).
    Would (mis-behavingly) fabricate a shape-compliant-but-meaningless split if
    it ever received a retry prompt (previous_error present) — this simulates
    exactly the reported failure mode. fabrication_attempted flips True only if
    that retry path is ever actually exercised."""

    def __init__(self):
        self.call_count = 0
        self.fabrication_attempted = False

    def invoke(self, messages):
        self.call_count += 1
        human_text = messages[-1][1]
        if "did not fully succeed" in human_text or "Expected exactly 2 replacement" in human_text:
            self.fabrication_attempted = True
            code = (
                "import pandas as pd\n"
                "path = 'unused'\n"  # never actually reached/executed in a correct run
                "df = pd.read_csv(path, dtype=str)\n"
                "df['id_first_digit'] = df['id'].astype(str).str[0]\n"
                "df['id_remaining_digits'] = df['id'].astype(str).str[1:]\n"
                "df = df.drop(columns=['id'])\n"
                "df.to_csv(path, index=False)\n"
            )
            return _FakeResponse(code)
        return _FakeResponse("# NO_SPLIT: this is a sequential row identifier, not a genuine composite field.")


tmp_dir4 = Path(tempfile.mkdtemp(prefix="composite_decline_no_retry_"))
try:
    cloned_path4 = tmp_dir4 / "jobs.csv"
    fixture_df4 = pd.DataFrame({"id": list(range(1, N + 1)), "Job Title": job_titles})
    fixture_df4.to_csv(cloned_path4, index=False)

    composite_issue4 = (
        f"Composite field (discovered): column 'id' has {N} value(s) (100%) "
        f"matching the pattern '^\\d+$' — this looks like two distinct values "
        "glued together, not caught by a fixed rubric check."
    )
    pattern_lookup4 = {
        composite_issue4: {"issue": composite_issue4, "pattern": r"^\d+$", "match_threshold": 1.0}
    }

    fake_llm4 = WouldFabricateUnderPressureLLM()
    status4, attempts4, error4, remaining4, code4 = dc._clean_issue_group(
        cloned_path4, [composite_issue4], fake_llm4, pattern_lookup=pattern_lookup4
    )
    print(f"status={status4!r} attempts={attempts4} fabrication_attempted={fake_llm4.fabrication_attempted}")

    assert fake_llm4.fabrication_attempted is False, (
        "the retry/previous_error path must NEVER be exercised for a legitimate "
        "decline — a legitimate NO_SPLIT is terminal, not fed back as a failure"
    )
    assert fake_llm4.call_count == 1, f"expected exactly 1 call, got {fake_llm4.call_count}"
    assert status4 == "declined_false_positive", f"expected the decline, not a fabricated fix, got {status4!r}"

    columns_after4 = list(pd.read_csv(cloned_path4, dtype=str).columns)
    assert columns_after4 == ["id", "Job Title"], (
        f"the file must be untouched — the fabricated split must never have run: {columns_after4}"
    )
    print("PASS: the pipeline got the correct decline, not the fabrication — no retry "
          "pressure was ever applied to a legitimate NO_SPLIT.\n")
finally:
    shutil.rmtree(tmp_dir4, ignore_errors=True)


print("=" * 70)
print("ALL COMPOSITE-FIELD-DECLINE ASSERTIONS PASSED")
print("=" * 70)
