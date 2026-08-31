"""Standalone test: router_node classification only (no sub-agent dispatch).

Confirms a clearly SQL-shaped question classifies as "sql_analyst" and a clearly
ETL-shaped request ("pull data from this URL") classifies as "etl_analyst", using
real LLM calls (pick_llm("cheap") via with_structured_output(RouterSchema)) — no
mocking, since this is testing the router's actual classification quality.
"""

from langchain_core.messages import HumanMessage

from agents.router import router_node
from models.router_schema import DataAgentSchema

print("=" * 70)
print("CASE 1: clearly SQL-shaped question")
print("=" * 70)
sql_state = DataAgentSchema(
    messages=[HumanMessage(content="How many orders came from São Paulo?")]
)
sql_result = router_node(sql_state)
print("route_response:", sql_result["route_response"])
print("route_comments:", sql_result["route_comments"])
assert sql_result["route_response"] == "sql_analyst", (
    f"expected 'sql_analyst', got {sql_result['route_response']!r}"
)
assert sql_result["route_comments"], "expected non-empty reasoning comments"
print("PASS: SQL-shaped question classified as 'sql_analyst'.\n")

print("=" * 70)
print("CASE 2: clearly ETL-shaped request")
print("=" * 70)
etl_state = DataAgentSchema(
    messages=[
        HumanMessage(
            content="Pull data from this URL: https://example.com/data.csv and load it "
            "into a local folder."
        )
    ]
)
etl_result = router_node(etl_state)
print("route_response:", etl_result["route_response"])
print("route_comments:", etl_result["route_comments"])
assert etl_result["route_response"] == "etl_analyst", (
    f"expected 'etl_analyst', got {etl_result['route_response']!r}"
)
assert etl_result["route_comments"], "expected non-empty reasoning comments"
print("PASS: ETL-shaped request classified as 'etl_analyst'.\n")

print("=" * 70)
print("ALL ROUTER_NODE CLASSIFICATION ASSERTIONS PASSED")
print("=" * 70)
