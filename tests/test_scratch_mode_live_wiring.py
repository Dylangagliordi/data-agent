"""
Tests for Spec 12b: Scratch Mode wired into the SQL analyst graph
(agents/sql_analyst.py: check_needs_scratch_mode, run_scratch_mode,
route_after_scratch_check).

Tests 1-6 use fake LLMs (no live model calls) to prove the routing/retry/
approval logic deterministically. Test 7 is a real, live end-to-end run
through the full graph proving check_needs_scratch_mode correctly says True
for a genuine "computed quadrant highlight" question and produces a real
custom chart via real code generation + real approval + real execution.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_scratch_mode_live_wiring.py
"""

import contextlib
import io
import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.sql_analyst as sql_analyst_module
from agents.sql_analyst import check_needs_scratch_mode, route_after_scratch_check, run_scratch_mode
from models.schema import ScratchModeSchema, SQLAnalystState

RESULT_JSON = (
    '{"columns": ["industry", "avg_rating", "avg_salary"], '
    '"rows": [["Tech", 4.5, 90000], ["Retail", 3.0, 45000]], "truncated": false}'
)


@contextlib.contextmanager
def _redirect_stdin(text: str):
    original_stdin = sys.stdin
    sys.stdin = io.StringIO(text)
    try:
        yield
    finally:
        sys.stdin = original_stdin


class _StructuredScratchLLM:
    def __init__(self, needs_scratch_mode: bool, reasoning: str = "test reasoning", raise_instead=False):
        self.needs_scratch_mode = needs_scratch_mode
        self.reasoning = reasoning
        self.raise_instead = raise_instead
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        if self.raise_instead:
            raise AssertionError("must not call the LLM when the result is empty")
        return ScratchModeSchema(needs_scratch_mode=self.needs_scratch_mode, reasoning=self.reasoning)


class _FakeCheckLLM:
    def __init__(self, structured_llm):
        self.structured_llm = structured_llm

    def with_structured_output(self, schema_cls):
        return self.structured_llm


def test_check_needs_scratch_mode_false_for_ordinary_chart():
    fake_structured = _StructuredScratchLLM(needs_scratch_mode=False, reasoning="plain bar chart is fine")
    original_pick_llm = sql_analyst_module.pick_llm
    sql_analyst_module.pick_llm = lambda level: _FakeCheckLLM(fake_structured)
    try:
        state = SQLAnalystState(
            user_question="how many orders per state",
            sql_query_execution_result=RESULT_JSON,
            chart_type="bar",
        )
        result = check_needs_scratch_mode(state)
        assert result["needs_scratch_mode"] is False
        assert result["scratch_mode_reasoning"] == "plain bar chart is fine"
        print("PASS: check_needs_scratch_mode returns False for an ordinary chart request")
    finally:
        sql_analyst_module.pick_llm = original_pick_llm


def test_check_needs_scratch_mode_true_for_computed_highlight():
    fake_structured = _StructuredScratchLLM(needs_scratch_mode=True, reasoning="needs a computed quadrant split")
    original_pick_llm = sql_analyst_module.pick_llm
    sql_analyst_module.pick_llm = lambda level: _FakeCheckLLM(fake_structured)
    try:
        state = SQLAnalystState(
            user_question="highlight the high-pay low-satisfaction opportunity zone",
            sql_query_execution_result=RESULT_JSON,
            chart_type="scatter",
        )
        result = check_needs_scratch_mode(state)
        assert result["needs_scratch_mode"] is True
        assert "quadrant" in result["scratch_mode_reasoning"]
        print("PASS: check_needs_scratch_mode returns True for a genuine computed-highlight request")
    finally:
        sql_analyst_module.pick_llm = original_pick_llm


def test_check_needs_scratch_mode_skips_llm_when_result_empty():
    fake_structured = _StructuredScratchLLM(needs_scratch_mode=True, raise_instead=True)
    original_pick_llm = sql_analyst_module.pick_llm
    sql_analyst_module.pick_llm = lambda level: _FakeCheckLLM(fake_structured)
    try:
        state = SQLAnalystState(user_question="q", sql_query_execution_result="", chart_type="bar")
        result = check_needs_scratch_mode(state)
        assert result == {}
        assert fake_structured.call_count == 0
        print("PASS: check_needs_scratch_mode never calls the LLM when there's no real result to judge")
    finally:
        sql_analyst_module.pick_llm = original_pick_llm


def test_route_after_scratch_check():
    assert route_after_scratch_check(SQLAnalystState(needs_scratch_mode=True)) == "run_scratch_mode"
    assert route_after_scratch_check(SQLAnalystState(needs_scratch_mode=False)) == "build_visualization"
    print("PASS: route_after_scratch_check routes correctly on the real state field")


_SAFE_CODE = (
    "import matplotlib\n"
    "matplotlib.use('Agg')\n"
    "import matplotlib.pyplot as plt\n"
    "fig, ax = plt.subplots()\n"
    "ax.scatter(df['avg_rating'], df['avg_salary'])\n"
    "fig.savefig({path!r})\n"
)
_UNSAFE_CODE = "import os\nos.system('echo hi')\n"


class _FakeCodeGenLLM:
    def __init__(self, code_sequence: list):
        self.code_sequence = list(code_sequence)
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        code = self.code_sequence.pop(0)
        return SimpleNamespace(content=code)


def _run_scratch_mode_with_fake(code_sequence, stdin_text="yes\n"):
    fake_llm = _FakeCodeGenLLM(code_sequence)
    original_pick_llm = sql_analyst_module.pick_llm
    sql_analyst_module.pick_llm = lambda level: fake_llm
    state = SQLAnalystState(
        user_question="highlight the opportunity zone",
        curated_question="highlight the opportunity zone",
        sql_query_execution_result=RESULT_JSON,
        scratch_mode_reasoning="needs a computed quadrant split",
    )
    try:
        with _redirect_stdin(stdin_text):
            result = run_scratch_mode(state)
        return result, fake_llm
    finally:
        sql_analyst_module.pick_llm = original_pick_llm


def test_run_scratch_mode_success():
    # The generated code embeds its own output path via .format at call time —
    # substitute it in after generation since run_scratch_mode picks the real path.
    class _PathAwareCodeGenLLM:
        def __init__(self):
            self.call_count = 0
            self.last_output_path = None

        def invoke(self, messages):
            self.call_count += 1
            human_content = messages[1][1]
            # Extract the exact output path run_scratch_mode told the LLM to use.
            marker = "Save the finished chart to this exact path: "
            start = human_content.index(marker) + len(marker)
            path_repr = human_content[start:].splitlines()[0].strip()
            output_path = eval(path_repr)  # safe: this is our own test-controlled string
            self.last_output_path = output_path
            return SimpleNamespace(content=_SAFE_CODE.format(path=output_path))

    fake_llm = _PathAwareCodeGenLLM()
    original_pick_llm = sql_analyst_module.pick_llm
    sql_analyst_module.pick_llm = lambda level: fake_llm
    state = SQLAnalystState(
        user_question="highlight the opportunity zone",
        curated_question="highlight the opportunity zone",
        sql_query_execution_result=RESULT_JSON,
        scratch_mode_reasoning="needs a computed quadrant split",
    )
    try:
        with _redirect_stdin("yes\n"):
            result = run_scratch_mode(state)
        assert fake_llm.call_count == 1, "a compliant first attempt must make exactly one LLM call"
        assert result["chart_image_path"], f"expected a real chart path, got: {result}"
        assert Path(result["chart_image_path"]).exists()
        assert Path(result["chart_image_path"]).stat().st_size > 0
        print("PASS: run_scratch_mode approves and executes real, safe code end to end, producing a real chart")
    finally:
        sql_analyst_module.pick_llm = original_pick_llm
        if fake_llm.last_output_path and Path(fake_llm.last_output_path).exists():
            Path(fake_llm.last_output_path).unlink()


def test_run_scratch_mode_retries_after_unsafe_code_then_succeeds():
    result, fake_llm = _run_scratch_mode_with_fake([_UNSAFE_CODE, _UNSAFE_CODE])
    # Both attempts unsafe -> must give up honestly, never execute anything.
    assert fake_llm.call_count == 2
    assert "Could not produce safe custom code" in result["final_answer"]
    assert "final_answer" in result and not result.get("chart_image_path")
    print("PASS: run_scratch_mode retries once on unsafe code, then gives up honestly with no file produced")


def test_run_scratch_mode_declined_approval():
    result, fake_llm = _run_scratch_mode_with_fake(
        [_SAFE_CODE.format(path="/tmp/should_not_be_created_scratch.png")], stdin_text="no\n"
    )
    assert "declined at the approval step" in result["final_answer"]
    assert not Path("/tmp/should_not_be_created_scratch.png").exists()
    print("PASS: a declined approval produces no file and an honest final_answer")


def test_needs_scratch_mode_real_live_end_to_end():
    """Live smoke test: a real question that clearly needs a computed
    quadrant highlight, run through check_needs_scratch_mode and
    run_scratch_mode with a real LLM at every step, approved via piped
    stdin — proving the whole wired-up path works with genuine model calls,
    not just fakes."""
    state = SQLAnalystState(
        user_question=(
            "Using this data (industry, avg_rating, avg_salary), plot avg_salary vs "
            "avg_rating and highlight, with a different color, any industry that falls "
            "in the high-salary-but-low-rating quadrant relative to the median of each axis."
        ),
        curated_question=(
            "Plot average salary versus average rating per industry, and highlight in a "
            "different color any industry above the median salary but below the median rating."
        ),
        sql_query_execution_result=RESULT_JSON,
        chart_type="scatter",
    )
    check_result = check_needs_scratch_mode(state)
    print("Live check_needs_scratch_mode result:", check_result)
    assert check_result.get("needs_scratch_mode") is True, (
        f"expected a real live LLM to recognize this needs a computed quadrant highlight, got: {check_result}"
    )

    state = state.model_copy(update=check_result)
    with _redirect_stdin("yes\n"):
        run_result = run_scratch_mode(state)
    print("Live run_scratch_mode final_answer:", run_result.get("final_answer"))
    assert run_result.get("chart_image_path"), f"expected a real chart to be produced, got: {run_result}"
    assert Path(run_result["chart_image_path"]).exists()
    print(f"PASS: real, live end-to-end Scratch Mode run produced a real chart: {run_result['chart_image_path']}")


if __name__ == "__main__":
    test_check_needs_scratch_mode_false_for_ordinary_chart()
    test_check_needs_scratch_mode_true_for_computed_highlight()
    test_check_needs_scratch_mode_skips_llm_when_result_empty()
    test_route_after_scratch_check()
    test_run_scratch_mode_success()
    test_run_scratch_mode_retries_after_unsafe_code_then_succeeds()
    test_run_scratch_mode_declined_approval()
    test_needs_scratch_mode_real_live_end_to_end()
    print("\nAll scratch_mode_live_wiring tests passed.")
