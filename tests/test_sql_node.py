"""Standalone test: sql_node's absolute import + dispatch, run from OUTSIDE the
agents/ package (as `python -m tests.test_sql_node`, invoked from the project
root) — this directly exercises the exact failure mode named in the spec: a flat
or relative import inside agents/router.py would fail here, since this test's
own module lives under tests/, not agents/, and main.py itself lives at the
project root one level above agents/.

Confirms:
- `from agents.sql_analyst import build_sql_analyst_graph` (agents/router.py's
  own import line) resolves correctly when imported this way.
- sql_node dispatches a real question to the real SQL analyst graph and returns
  a real final_answer via this schema's own final_answer field.
"""

from langchain_core.messages import HumanMessage

from agents.router import sql_node
from models.router_schema import DataAgentSchema

print("=" * 70)
print("Confirming absolute import resolves from outside agents/")
print("=" * 70)
# If agents/router.py used a flat `import sql_analyst` or a relative
# `from . import sql_analyst` that happened to only work by accident when run
# from inside agents/, importing agents.router itself (done at module load,
# above) would already have failed before this line even runs.
print("PASS: `import agents.router` succeeded (absolute import resolved).\n")

print("=" * 70)
print("Dispatching a real SQL-shaped question through sql_node")
print("=" * 70)
state = DataAgentSchema(messages=[HumanMessage(content="How many orders are in the database in total?")])
result = sql_node(state)
print("final_answer:", result["final_answer"])

assert "final_answer" in result and result["final_answer"], "expected a non-empty final_answer"
assert not result["final_answer"].startswith("The SQL analyst sub-agent failed"), (
    f"expected a real answer, not a failure message: {result['final_answer']}"
)
print("PASS: sql_node dispatched to the real SQL analyst graph and returned a real answer.\n")

print("=" * 70)
print("ALL sql_node ABSOLUTE-IMPORT + DISPATCH ASSERTIONS PASSED")
print("=" * 70)
