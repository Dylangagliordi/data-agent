"""
Live system self-map (Spec 1): renders the real, currently-compiled structure
of any of this project's three LangGraph graphs, instead of relying on a
checked-in PNG that silently goes stale the moment a node is added or removed.

CLI trigger: `python main.py "map"` (see main.py). See tests/test_system_map.py.
"""

from pathlib import Path


def _get_graph_builders() -> dict:
    """Imported lazily so a broken import in one graph module never blocks
    generating the other two, and so importing this module never pays for
    LangChain/LangGraph setup until a map is actually requested.
    """
    from agents.data_agent import build_data_agent_graph
    from agents.etl_analyst import build_etl_analyst_graph
    from agents.sql_analyst import build_sql_analyst_graph

    return {
        "sql_analyst": build_sql_analyst_graph,
        "etl_analyst": build_etl_analyst_graph,
        "data_agent": build_data_agent_graph,
    }


def generate_system_map(graph_name: str) -> bytes:
    """Compile graph_name fresh (never a cached/stale reference) and return its
    real, current structure as mermaid-rendered PNG bytes.

    graph_name must be one of "sql_analyst", "etl_analyst", "data_agent".
    Building and introspecting a graph never executes any node's body, so this
    never opens a database connection or calls an LLM.
    """
    builders = _get_graph_builders()
    if graph_name not in builders:
        valid = ", ".join(sorted(builders))
        raise ValueError(f"unknown graph_name {graph_name!r} — must be one of: {valid}")
    graph = builders[graph_name]()
    return graph.get_graph().draw_mermaid_png()


def generate_all_system_maps(out_dir: str = ".") -> dict:
    """Regenerate all three graphs' PNGs into out_dir (created if missing).

    Returns {graph_name: written_path}. File names match this project's
    existing convention: "<graph_name>_graph.png".
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    written = {}
    for graph_name in _get_graph_builders():
        png_bytes = generate_system_map(graph_name)
        dest = out_path / f"{graph_name}_graph.png"
        dest.write_bytes(png_bytes)
        written[graph_name] = str(dest)
    return written
