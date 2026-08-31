"""Standalone test: deliberately force an unexpected exception inside a
sub-agent's invocation, and confirm sql_node/etl_node report a clean failure
via final_answer rather than crashing the router itself.

Monkeypatches the module-level compiled graph objects (agents.router's
_SQL_ANALYST_GRAPH / _ETL_ANALYST_GRAPH) with a fake object whose .invoke()
raises a real, deliberate exception — simulating the "broken import" / any
unexpected sub-agent crash scenario from the spec without needing to actually
break an import (which would also break every other test in this suite that
imports agents.router).
"""

import agents.router as router_module
from langchain_core.messages import HumanMessage
from models.router_schema import DataAgentSchema


class ExplodingGraph:
    """Stand-in for a compiled sub-agent graph whose .invoke() always raises —
    simulates any unexpected internal failure (a broken import surfacing at call
    time, an uncaught internal error, etc.), not one of the sub-agent's own
    already-handled cases (like a caught DB error or a declined cleaning step)."""

    def invoke(self, *args, **kwargs):
        raise RuntimeError("deliberate forced failure to simulate a broken sub-agent")


print("=" * 70)
print("CASE 1: sql_node — forced exception inside the SQL analyst sub-agent")
print("=" * 70)

original_sql_graph = router_module._SQL_ANALYST_GRAPH
router_module._SQL_ANALYST_GRAPH = ExplodingGraph()
try:
    state = DataAgentSchema(messages=[HumanMessage(content="how many orders are there")])
    result = router_module.sql_node(state)
finally:
    router_module._SQL_ANALYST_GRAPH = original_sql_graph

print("final_answer:", result["final_answer"])
assert "final_answer" in result and result["final_answer"], "expected a non-empty final_answer"
assert "SQL analyst" in result["final_answer"], (
    f"expected the failure message to name the SQL analyst sub-agent: {result['final_answer']}"
)
assert "deliberate forced failure" in result["final_answer"], (
    f"expected the real error text in the failure message: {result['final_answer']}"
)
print("PASS: sql_node caught the exception and reported a clean, naming failure message.\n")


print("=" * 70)
print("CASE 2: etl_node — forced exception inside the ETL analyst sub-agent")
print("=" * 70)

original_etl_graph = router_module._ETL_ANALYST_GRAPH
router_module._ETL_ANALYST_GRAPH = ExplodingGraph()
try:
    state = DataAgentSchema(
        messages=[HumanMessage(content="download data from https://example.com/data.csv")]
    )
    result2 = router_module.etl_node(state)
finally:
    router_module._ETL_ANALYST_GRAPH = original_etl_graph

print("final_answer:", result2["final_answer"])
assert "final_answer" in result2 and result2["final_answer"], "expected a non-empty final_answer"
assert "ETL analyst" in result2["final_answer"], (
    f"expected the failure message to name the ETL analyst sub-agent: {result2['final_answer']}"
)
assert "deliberate forced failure" in result2["final_answer"], (
    f"expected the real error text in the failure message: {result2['final_answer']}"
)
print("PASS: etl_node caught the exception and reported a clean, naming failure message.\n")

print("=" * 70)
print("ALL EXCEPTION-HANDLING (NEITHER NODE CRASHED THE ROUTER) ASSERTIONS PASSED")
print("=" * 70)
