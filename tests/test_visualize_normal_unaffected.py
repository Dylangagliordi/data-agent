"""Confirm a normal ask: question is completely unaffected by the visualization changes.

Runs a plain SQL question through sql_node and verifies:
- represent_final_answer is still used (no output_file_path set)
- chart_type fields remain at their defaults (empty/False)
- final_answer is a plain-English answer, not a visualization message
"""

from langchain_core.messages import HumanMessage

import agents.router as router_module
from agents.router import sql_node
from models.router_schema import DataAgentSchema

print("=" * 70)
print("Normal SQL question through sql_node (not visualize_node)")
print("=" * 70)

state = DataAgentSchema(
    messages=[HumanMessage(content="How many total orders are in the database?")]
)
result = sql_node(state)
print("final_answer:", result["final_answer"])
print()

# Verify it's a plain-English answer, not a visualization message
assert result["final_answer"], "final_answer must be non-empty"
assert "Visualization data saved to:" not in result["final_answer"], (
    "normal sql_node must NOT produce a 'Visualization data saved to:' message"
)
assert "Chart type:" not in result["final_answer"], (
    "normal sql_node must NOT produce a 'Chart type:' line"
)
print("PASS: final_answer is a plain-English answer, not a visualization message.\n")

# Verify visualization state fields remain at defaults in the sub-agent result
sql_state = router_module.LAST_SQL_ANALYST_STATE
print(f"wants_visualization in sub-agent state: {sql_state.get('wants_visualization')}")
print(f"chart_type in sub-agent state: {sql_state.get('chart_type')!r}")
print(f"output_file_path in sub-agent state: {sql_state.get('output_file_path')!r}")

assert sql_state.get("wants_visualization") is False, (
    "wants_visualization must be False for a normal sql_node dispatch"
)
assert sql_state.get("chart_type") == "", (
    f"chart_type must be empty string for a normal question, got {sql_state.get('chart_type')!r}"
)
assert sql_state.get("output_file_path") == "", (
    f"output_file_path must be empty string for a normal question, got {sql_state.get('output_file_path')!r}"
)
print("PASS: visualization state fields at defaults — sql_node is completely unaffected.\n")

print("=" * 70)
print("NORMAL SQL UNAFFECTED ASSERTIONS PASSED")
print("=" * 70)
