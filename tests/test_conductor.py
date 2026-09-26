"""
Tests for Spec 16: Fully Agentic Conductor (agents/conductor.py).

Same fake-tool-calling-LLM convention tests/test_etl_recursion_limit.py
already established for this exact ReAct shape — a fake LLM that forces a
specific, deterministic tool-calling pattern, monkeypatching
agents.conductor.pick_llm (imported locally inside call_model, so patching
utils.llm_pick.pick_llm directly, the same technique test_transform_load_
recipe_cache.py already uses for exactly this "no llm= injection point"
situation).

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_conductor.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langchain_core.messages import AIMessage

import utils.llm_pick as llm_pick_module
from agents.conductor import CONDUCTOR_TOOLS, ask_question, run_conductor


class _FixedSequenceLLM:
    """bind_tools() returns self; each .invoke() call returns the next
    scripted AIMessage in the sequence — a real, deterministic multi-step
    ReAct trace, never a live model."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.call_count = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.call_count += 1
        return self.responses.pop(0)


def _tool_call_message(name, args, call_id):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": call_id}])


def test_ask_question_calls_the_real_unchanged_run_question():
    """ask_question must have no bypass of its own — it calls the exact same
    utils.cli_modes.run_question every other entry point already uses, so any
    approval gate a question redirects into (clean_and_reload, Scratch Mode)
    is the same, real, unchanged gate regardless of who calls it."""
    import utils.cli_modes as cli_modes_module

    original_run_question = cli_modes_module.run_question
    calls = []

    def _fake_run_question(question):
        calls.append(question)
        return {"final_answer": "42 orders."}

    cli_modes_module.run_question = _fake_run_question
    try:
        result = ask_question.invoke({"question": "how many orders"})
        assert result == "42 orders."
        assert calls == ["how many orders"]
        print("PASS: ask_question delegates to the real, unchanged run_question with no bypass")
    finally:
        cli_modes_module.run_question = original_run_question


def test_run_conductor_simple_case_one_tool_call():
    fake_llm = _FixedSequenceLLM(
        [
            _tool_call_message("ask_question", {"question": "how many orders came from São Paulo"}, "call_1"),
            AIMessage(content="There were 15,540 orders from São Paulo."),
        ]
    )
    original_pick_llm = llm_pick_module.pick_llm
    llm_pick_module.pick_llm = lambda level: fake_llm

    import utils.cli_modes as cli_modes_module

    original_run_question = cli_modes_module.run_question
    cli_modes_module.run_question = lambda q: {"final_answer": "There were 15,540 orders from São Paulo."}

    try:
        result = run_conductor("how many orders came from São Paulo")
        assert result["final_answer"] == "There were 15,540 orders from São Paulo."
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["tool"] == "ask_question"
        assert result["tool_calls"][0]["input"] == {"question": "how many orders came from São Paulo"}
        assert "15,540" in result["tool_calls"][0]["output"]
        print("PASS: a simple goal makes exactly one tool call and returns the real trace")
    finally:
        llm_pick_module.pick_llm = original_pick_llm
        cli_modes_module.run_question = original_run_question


def test_run_conductor_multi_step_real_ordered_trace():
    """A genuinely multi-step goal: profile a table, then check joins, then
    ask a real question — the trace must reflect the real order and real
    inputs/outputs of each, not just a count."""
    fake_llm = _FixedSequenceLLM(
        [
            _tool_call_message("profile_table", {"table_name": "orders"}, "call_1"),
            _tool_call_message("check_joins", {}, "call_2"),
            _tool_call_message("ask_question", {"question": "what is the average order value"}, "call_3"),
            AIMessage(content="Based on the profile, join map, and query: the average order value is $137."),
        ]
    )
    original_pick_llm = llm_pick_module.pick_llm
    llm_pick_module.pick_llm = lambda level: fake_llm

    import utils.auto_eda as auto_eda_module
    import utils.cli_modes as cli_modes_module
    import utils.join_advisory as join_advisory_module

    original_profile = auto_eda_module.render_auto_eda_html
    original_joins = join_advisory_module.render_join_advisory_html
    original_run_question = cli_modes_module.run_question

    auto_eda_module.render_auto_eda_html = lambda table_name: f"/fake/profile_{table_name}.html"
    join_advisory_module.render_join_advisory_html = lambda: "/fake/joins.html"
    cli_modes_module.run_question = lambda q: {"final_answer": "The average order value is $137."}

    try:
        result = run_conductor("give me a full picture of orders and the average order value")
        trace = result["tool_calls"]
        assert [t["tool"] for t in trace] == ["profile_table", "check_joins", "ask_question"], (
            f"expected the real, scripted order to be preserved in the trace, got: {trace}"
        )
        assert trace[0]["input"] == {"table_name": "orders"}
        assert "profile_orders.html" in trace[0]["output"]
        assert "joins.html" in trace[1]["output"]
        assert "$137" in trace[2]["output"]
        assert "$137" in result["final_answer"]
        print("PASS: a real multi-step goal produces a real, correctly-ordered trace across 3 different tools")
    finally:
        llm_pick_module.pick_llm = original_pick_llm
        auto_eda_module.render_auto_eda_html = original_profile
        join_advisory_module.render_join_advisory_html = original_joins
        cli_modes_module.run_question = original_run_question


def test_run_conductor_step_cap_stops_cleanly():
    """Forces a scenario that can never decide to stop — the real failure
    mode the step cap exists to guard against — same discipline as
    test_etl_recursion_limit.py."""

    class InfiniteToolCallLLM:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return _tool_call_message("check_dq_backlog", {}, "call_forced_loop")

    original_pick_llm = llm_pick_module.pick_llm
    llm_pick_module.pick_llm = lambda level: InfiniteToolCallLLM()

    import utils.dq_backlog as dq_backlog_module

    original_dq = dq_backlog_module.render_dq_backlog_html
    dq_backlog_module.render_dq_backlog_html = lambda: "/fake/dq_backlog.html"

    try:
        SMALL_MAX_STEPS = 4
        result = run_conductor("this will never complete", max_steps=SMALL_MAX_STEPS)
        assert result["final_answer"].startswith("Stopped after"), result
        assert str(SMALL_MAX_STEPS) in result["final_answer"]
        assert result["tool_calls"] == [], "a recursion-limit bailout has no completed trace to report"
        print("PASS: the step cap stops an infinite tool-calling pattern cleanly, with a clear message")
    finally:
        llm_pick_module.pick_llm = original_pick_llm
        dq_backlog_module.render_dq_backlog_html = original_dq


def test_all_conductor_tools_are_real_and_documented():
    """Every registered tool has a real docstring (the LLM's only way to know
    what a tool does and when to use it) — never a placeholder."""
    for t in CONDUCTOR_TOOLS:
        assert t.description and len(t.description) > 20, f"{t.name} has no real description"
    names = [t.name for t in CONDUCTOR_TOOLS]
    assert len(names) == len(set(names)), "duplicate tool names would confuse the model's tool-calling"
    print(f"PASS: all {len(CONDUCTOR_TOOLS)} conductor tools are real, documented, and uniquely named")


def test_run_conductor_real_live_multi_step():
    """Live smoke test, no fakes anywhere: a genuine goal against the real
    olist_sellers_dataset table, with a real model at every reasoning step —
    same "one real end-to-end proof alongside the fakes" precedent
    tests/test_etl_full_loop.py already established for this exact ReAct
    shape."""
    result = run_conductor(
        "Give me a quick sense of the olist_sellers_dataset table, then tell me how many rows it has.",
        max_steps=6,
    )
    assert not result["final_answer"].startswith("Stopped after"), (
        f"expected a real completion within the step limit, got: {result['final_answer']}"
    )
    assert result["tool_calls"], "expected at least one real tool call for a genuine investigative goal"
    assert "3,095" in result["final_answer"] or "3095" in result["final_answer"], (
        f"expected the real row count to appear in the final answer, got: {result['final_answer']}"
    )
    print(f"PASS: real, live multi-step run completed with {len(result['tool_calls'])} real tool calls "
          f"and the correct real row count in its final answer")


if __name__ == "__main__":
    test_ask_question_calls_the_real_unchanged_run_question()
    test_run_conductor_simple_case_one_tool_call()
    test_run_conductor_multi_step_real_ordered_trace()
    test_run_conductor_step_cap_stops_cleanly()
    test_all_conductor_tools_are_real_and_documented()
    test_run_conductor_real_live_multi_step()
    print("\nAll conductor tests passed.")
