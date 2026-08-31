"""Full data_agent graph, end to end: a real ETL-shaped request (a real
downloadable URL).

Confirms:
- The router correctly classifies it as "etl_analyst" (route_response).
- The graph actually routes to etl_node (not sql_node), a real file gets
  downloaded, and the graph produces a real, clean final_answer.
- route_response is still the original classification after the sub-agent ran.
"""

import shutil
from pathlib import Path

from langchain_core.messages import HumanMessage

from agents.data_agent import build_data_agent_graph
from models.router_schema import DataAgentSchema

OUTPUT_FOLDER = "data/_test_etl/full_graph_etl_test"
shutil.rmtree(OUTPUT_FOLDER, ignore_errors=True)

graph = build_data_agent_graph()

request = (
    "Please download this file from "
    "https://raw.githubusercontent.com/pandas-dev/pandas/main/README.md "
    f"and save it into the folder {OUTPUT_FOLDER} as md."
)
print("REQUEST:", request)
print("=" * 70)

result = graph.invoke(DataAgentSchema(messages=[HumanMessage(content=request)]))

print("route_response:", result["route_response"])
print("route_comments:", result["route_comments"])
print("final_answer:", result["final_answer"])

assert result["route_response"] == "etl_analyst", (
    f"expected routing to etl_analyst, got {result['route_response']!r}"
)
print("PASS: routed to etl_analyst.")

assert result["final_answer"], "expected a non-empty final_answer"
assert not result["final_answer"].startswith("The ETL analyst sub-agent failed"), (
    f"expected a real answer, not a failure message: {result['final_answer']}"
)
print("PASS: produced a real, clean final_answer.")

downloaded = list(Path(OUTPUT_FOLDER).glob("*"))
assert downloaded, f"expected a real downloaded file in {OUTPUT_FOLDER}, found none"
print(f"PASS: real file actually downloaded: {downloaded}\n")

print("ALL FULL-GRAPH ETL-SHAPED END-TO-END ASSERTIONS PASSED")
