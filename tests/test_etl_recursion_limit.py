"""Standalone test: the ETL analyst's recursion limit stops a looping decision pattern
cleanly, with a clear message, rather than running unbounded or crashing.

Deterministically constructs a scenario likely to loop: monkeypatches
agents.etl_analyst.pick_llm to return a fake LLM that ALWAYS requests another tool
call (extract_load against a bad URL) and never stops — the real failure mode the
recursion limit exists to guard against, forced rather than hoped-for.

Uses a small max_steps so the test runs fast without waiting through 15 real steps.
"""

import agents.etl_analyst as etl_analyst
from langchain_core.messages import AIMessage


class InfiniteToolCallLLM:
    """bind_tools() returns self (so call_model's .bind_tools(...) call works); every
    .invoke() returns an AIMessage with a tool_call requesting extract_load again — this
    LLM can never decide to stop, by construction."""

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "extract_load",
                    "args": {
                        "url": "https://example.invalid/never-stops.csv",
                        "output_folder": "data/_test_etl/never_used",
                        "format": "csv",
                    },
                    "id": "call_forced_loop",
                }
            ],
        )


original_pick_llm = etl_analyst.pick_llm
etl_analyst.pick_llm = lambda level: InfiniteToolCallLLM()

try:
    SMALL_MAX_STEPS = 4
    result = etl_analyst.run_etl_analyst(
        "this will never complete, the fake LLM always requests another tool call",
        max_steps=SMALL_MAX_STEPS,
    )
finally:
    etl_analyst.pick_llm = original_pick_llm

print("RESULT:", result)

assert result.startswith("Stopped after"), f"expected the clear stop message, got: {result!r}"
assert str(SMALL_MAX_STEPS) in result, f"expected the step count ({SMALL_MAX_STEPS}) in the message: {result!r}"
print(f"\nPASS: recursion limit stopped the loop cleanly with a clear message "
      f"(no unbounded run, no raw exception surfaced).")

print("\nALL RECURSION-LIMIT ASSERTIONS PASSED")
