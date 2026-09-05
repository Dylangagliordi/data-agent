"""
Regression test for architecture review point #27: LAST_SQL_ANALYST_STATE used
to be a module-level dict — unsafe if two questions were ever handled
concurrently, since one request's trace could be overwritten by another's
before the first caller reads it.

The fix removes that global entirely: sql_node/visualize_node now return the
sub-agent's full internal state as `sql_analyst_trace` on DataAgentSchema
itself, threaded through the graph's normal state mechanism instead of shared
mutable module state.

Test 1 (structural): the module-level global no longer exists at all.
Test 2 (simulated concurrency): interleave two sql_node calls "by hand" using
fake sub-agent graphs standing in for _SQL_ANALYST_GRAPH — call A partway
through, call B fully, then finish call A — and confirm each call's returned
sql_analyst_trace holds ONLY its own data, never the other call's.
"""

import agents.router as router_module
from models.router_schema import DataAgentSchema
from langchain_core.messages import HumanMessage

print("=" * 70)
print("TEST 1: the module-level global no longer exists")
print("=" * 70)

assert not hasattr(router_module, "LAST_SQL_ANALYST_STATE"), (
    "LAST_SQL_ANALYST_STATE must be removed entirely — it was the unsafe "
    "shared mutable state this fix eliminates."
)
print("PASS: agents.router has no LAST_SQL_ANALYST_STATE attribute.\n")


print("=" * 70)
print("TEST 2: two REAL, genuinely overlapping sql_node calls in separate")
print("threads never cross-contaminate each other's trace")
print("=" * 70)

import threading
import time


class _FakeGraph:
    """Stands in for _SQL_ANALYST_GRAPH: invoke() sleeps briefly (so two
    threads' calls genuinely overlap in wall-clock time) then returns a result
    dict tagged with a distinct marker per call, so cross-contamination is
    unmistakable if it ever happens. With the old module-level global, thread
    B's invoke() completing while thread A's invoke() is still sleeping would
    have overwritten LAST_SQL_ANALYST_STATE out from under thread A.
    """

    def __init__(self, marker, sleep_seconds):
        self.marker = marker
        self.sleep_seconds = sleep_seconds

    def invoke(self, state, config=None):
        time.sleep(self.sleep_seconds)
        return {
            "final_answer": f"answer-for-{self.marker}",
            "curated_question": f"curated-{self.marker}",
            "generated_sql_query": f"SELECT '{self.marker}'",
            "is_safe": "yes",
            "sql_query_execution_result": f"result-{self.marker}",
        }


original_graph = router_module._SQL_ANALYST_GRAPH
results: dict = {}


def _run(marker: str, sleep_seconds: float, key: str) -> None:
    # Each thread call resolves its own graph via a thread-local swap window,
    # but the key point under test is sql_node's RETURN VALUE, not module
    # state — that's exactly what's now safe against interleaving.
    graph = _FakeGraph(marker, sleep_seconds)
    router_module._SQL_ANALYST_GRAPH = graph
    state = DataAgentSchema(messages=[HumanMessage(content=f"question {marker}")])
    results[key] = router_module.sql_node(state)


try:
    # Thread A sleeps longer, so thread B's invoke() call starts and finishes
    # entirely while thread A's own invoke() call is still in progress.
    thread_a = threading.Thread(target=_run, args=("request-A", 0.3, "a"))
    thread_b = threading.Thread(target=_run, args=("request-B", 0.0, "b"))
    thread_a.start()
    time.sleep(0.05)  # ensure thread A's invoke() has actually started sleeping
    thread_b.start()
    thread_a.join()
    thread_b.join()

    result_a, result_b = results["a"], results["b"]
    print(f"result_a.sql_analyst_trace: {result_a['sql_analyst_trace']}")
    print(f"result_b.sql_analyst_trace: {result_b['sql_analyst_trace']}")

    assert result_a["final_answer"] == "answer-for-request-A", (
        f"request A's final_answer was contaminated: {result_a['final_answer']!r}"
    )
    assert result_a["sql_analyst_trace"]["curated_question"] == "curated-request-A", (
        f"request A's trace was contaminated: {result_a['sql_analyst_trace']!r}"
    )
    assert result_b["final_answer"] == "answer-for-request-B", (
        f"request B's final_answer was contaminated: {result_b['final_answer']!r}"
    )
    assert result_b["sql_analyst_trace"]["curated_question"] == "curated-request-B", (
        f"request B's trace was contaminated: {result_b['sql_analyst_trace']!r}"
    )
    print("PASS: each thread's sql_analyst_trace held only its own data, even "
          "though thread B's call completed entirely before thread A's did.\n")
finally:
    router_module._SQL_ANALYST_GRAPH = original_graph

print("=" * 70)
print("ALL ROUTER-STATE-NO-GLOBAL ASSERTIONS PASSED")
print("=" * 70)
