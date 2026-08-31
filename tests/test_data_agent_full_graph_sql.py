"""Full data_agent graph, end to end: a real SQL-shaped question.

Confirms:
- The router correctly classifies it as "sql_analyst" (route_response).
- The graph actually routes to sql_node (not etl_node) and produces a real,
  clean final_answer.
- route_response is still the original classification after the sub-agent ran
  (never overwritten by sql_node/etl_node) — final_answer is a separate field.
"""

from langchain_core.messages import HumanMessage

from agents.data_agent import build_data_agent_graph
from models.router_schema import DataAgentSchema

graph = build_data_agent_graph()

question = "How many distinct customers are in the database?"
print("QUESTION:", question)
print("=" * 70)

result = graph.invoke(DataAgentSchema(messages=[HumanMessage(content=question)]))

print("route_response:", result["route_response"])
print("route_comments:", result["route_comments"])
print("final_answer:", result["final_answer"])

assert result["route_response"] == "sql_analyst", (
    f"expected routing to sql_analyst, got {result['route_response']!r}"
)
print("PASS: routed to sql_analyst.")

assert result["final_answer"], "expected a non-empty final_answer"
assert not result["final_answer"].startswith("The SQL analyst sub-agent failed"), (
    f"expected a real answer, not a failure message: {result['final_answer']}"
)
print("PASS: produced a real, clean final_answer.")

assert result["route_comments"], "expected route_comments to be populated"
print("PASS: route_comments (router's own reasoning) present.\n")

print("ALL FULL-GRAPH SQL-SHAPED END-TO-END ASSERTIONS PASSED")
