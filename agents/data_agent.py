"""
Top-level data_agent graph: the router dispatches every incoming message to
either the SQL analyst or the ETL analyst sub-agent automatically, instead of
either one being invoked by hand.

Graph shape:
    START -> router_node
    router_node --(router_edge)--> sql_node | etl_node
    sql_node -> END
    etl_node -> END
"""

from langgraph.graph import END, START, StateGraph

from agents.router import etl_node, router_edge, router_node, sql_node
from models.router_schema import DataAgentSchema


def build_data_agent_graph():
    """Wire router_node, sql_node, and etl_node into a StateGraph using
    DataAgentSchema, and compile it."""
    graph = StateGraph(DataAgentSchema)

    graph.add_node("router_node", router_node)
    graph.add_node("sql_node", sql_node)
    graph.add_node("etl_node", etl_node)

    graph.add_edge(START, "router_node")
    graph.add_conditional_edges(
        "router_node",
        router_edge,
        {"sql_analyst": "sql_node", "etl_analyst": "etl_node"},
    )
    graph.add_edge("sql_node", END)
    graph.add_edge("etl_node", END)

    return graph.compile()
