"""Standalone test: etl_node's absolute import + dispatch, run from OUTSIDE the
agents/ package (as `python -m tests.test_etl_node`, invoked from the project
root) — same absolute-import failure mode as test_sql_node.py, for the ETL side.

Confirms:
- `from agents.etl_analyst import build_etl_analyst_graph` (agents/router.py's
  own import line) resolves correctly when imported this way.
- etl_node dispatches a real request to the real ETL analyst graph and returns a
  real final_answer (the last AI message's content) via this schema's own
  final_answer field.

Run with:
    printf 'yes\\n' | uv run python -m tests.test_etl_node
(a real download-only request needs no approval gate, so no stdin should
actually be consumed here, but piping 'yes' guards against a hang if the
request is ever interpreted more broadly than intended.)
"""

import shutil
from pathlib import Path

from langchain_core.messages import HumanMessage

from agents.router import etl_node
from models.router_schema import DataAgentSchema

print("=" * 70)
print("Confirming absolute import resolves from outside agents/")
print("=" * 70)
print("PASS: `import agents.router` succeeded (absolute import resolved).\n")

OUTPUT_FOLDER = "data/_test_etl/router_node_test"
shutil.rmtree(OUTPUT_FOLDER, ignore_errors=True)

print("=" * 70)
print("Dispatching a real ETL-shaped request (download only) through etl_node")
print("=" * 70)
request = (
    "Download this file: "
    "https://raw.githubusercontent.com/pandas-dev/pandas/main/README.md "
    f"into the folder {OUTPUT_FOLDER} as md."
)
state = DataAgentSchema(messages=[HumanMessage(content=request)])
result = etl_node(state)
print("final_answer:", result["final_answer"])

assert "final_answer" in result and result["final_answer"], "expected a non-empty final_answer"
assert not result["final_answer"].startswith("The ETL analyst sub-agent failed"), (
    f"expected a real answer, not a failure message: {result['final_answer']}"
)
downloaded = list(Path(OUTPUT_FOLDER).glob("*"))
assert downloaded, f"expected a real downloaded file in {OUTPUT_FOLDER}, found none"
print(f"PASS: etl_node dispatched to the real ETL analyst graph, real file downloaded: {downloaded}\n")

print("=" * 70)
print("ALL etl_node ABSOLUTE-IMPORT + DISPATCH ASSERTIONS PASSED")
print("=" * 70)
