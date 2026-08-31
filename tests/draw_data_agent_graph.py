"""Compile the top-level data_agent (router) graph and render its mermaid diagram
to a PNG.

Usage:
    python tests/draw_data_agent_graph.py
Produces: data_agent_graph.png in the project root.
"""

from agents.data_agent import build_data_agent_graph

if __name__ == "__main__":
    graph = build_data_agent_graph()
    png_bytes = graph.get_graph().draw_mermaid_png()
    out_path = "data_agent_graph.png"
    with open(out_path, "wb") as f:
        f.write(png_bytes)
    print(f"Wrote {out_path} ({len(png_bytes)} bytes)")
