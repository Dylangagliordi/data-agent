"""
Top-level data_agent graph: the router dispatches every incoming message to
the SQL analyst, ETL analyst, or visualize sub-agent automatically.

Graph shape:
    START -> router_node
    router_node --(router_edge)--> sql_node | etl_node | visualize_node
    sql_node -> END
    etl_node -> END
    visualize_node -> END
"""

from langgraph.graph import END, START, StateGraph

from agents.router import etl_node, router_edge, router_node, sql_node, visualize_node
from models.router_schema import DataAgentSchema


def build_data_agent_graph():
    """Wire router_node, sql_node, etl_node, and visualize_node into a
    StateGraph using DataAgentSchema, and compile it."""
    graph = StateGraph(DataAgentSchema)

    graph.add_node("router_node", router_node)
    graph.add_node("sql_node", sql_node)
    graph.add_node("etl_node", etl_node)
    graph.add_node("visualize_node", visualize_node)

    graph.add_edge(START, "router_node")
    graph.add_conditional_edges(
        "router_node",
        router_edge,
        {
            "sql_analyst": "sql_node",
            "etl_analyst": "etl_node",
            "visualize": "visualize_node",
        },
    )
    graph.add_edge("sql_node", END)
    graph.add_edge("etl_node", END)
    graph.add_edge("visualize_node", END)

    return graph.compile()
