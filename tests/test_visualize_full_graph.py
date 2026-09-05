"""End-to-end test: full SQL analyst graph invoked via visualize_node.

Two cases:
1. Explicit chart type named in the question.
2. No chart type named (reasoned path).

Both must produce:
- A real output CSV file at a non-empty output_file_path.
- A non-empty final_answer that includes the file path and chart type.
- For case 2: chart_type_source="reasoned" with real reasoning in final_answer.
"""

from pathlib import Path

from agents.router import visualize_node
from models.router_schema import DataAgentSchema
from langchain_core.messages import HumanMessage

print("=" * 70)
print("CASE 1: explicit chart type in question")
print("=" * 70)
state1 = DataAgentSchema(
    messages=[HumanMessage(content="Give me a bar chart of the total number of orders per customer state.")]
)
result1 = visualize_node(state1)
print("final_answer:\n", result1["final_answer"])
print()

assert result1["final_answer"], "final_answer must be non-empty"
assert "Visualization data saved to:" in result1["final_answer"], (
    "final_answer must include 'Visualization data saved to:'"
)
assert "bar chart" in result1["final_answer"].lower(), "final_answer must mention bar chart"

# Extract file path and verify it exists
for line in result1["final_answer"].splitlines():
    if line.startswith("Visualization data saved to:"):
        file_path = line.split(":", 1)[1].strip()
        break
else:
    raise AssertionError("Could not find file path in final_answer")

assert Path(file_path).exists(), f"output CSV must exist at {file_path}"
print(f"PASS: explicit chart type -> CSV at {file_path}\n")

print("=" * 70)
print("CASE 2: no chart type named (reasoned path)")
print("=" * 70)
state2 = DataAgentSchema(
    messages=[HumanMessage(content="Visualize how order volume has changed month by month over the full dataset.")]
)
result2 = visualize_node(state2)
print("final_answer:\n", result2["final_answer"])
print()

assert result2["final_answer"], "final_answer must be non-empty"
assert "Visualization data saved to:" in result2["final_answer"], (
    "final_answer must include 'Visualization data saved to:'"
)

for line in result2["final_answer"].splitlines():
    if line.startswith("Visualization data saved to:"):
        file_path2 = line.split(":", 1)[1].strip()
        break
else:
    raise AssertionError("Could not find file path in final_answer for case 2")

assert Path(file_path2).exists(), f"output CSV must exist at {file_path2}"

# Access the internal state to verify chart_type_source
sql_state = result2.get("sql_analyst_trace", {})
print(f"chart_type: {sql_state.get('chart_type')}, source: {sql_state.get('chart_type_source')}")
print(f"chart_type_reasoning: {sql_state.get('chart_type_reasoning')}")

assert sql_state.get("chart_type_source") == "reasoned", (
    f"expected 'reasoned' when no chart type named, got {sql_state.get('chart_type_source')!r}"
)
assert sql_state.get("chart_type_reasoning"), "chart_type_reasoning must be non-empty for reasoned path"
assert "Chart type reasoning:" in result2["final_answer"], (
    "reasoned path -> reasoning must appear in final_answer"
)
print(f"PASS: reasoned chart type -> CSV at {file_path2}\n")

print("=" * 70)
print("ALL VISUALIZE FULL GRAPH ASSERTIONS PASSED")
print("=" * 70)
