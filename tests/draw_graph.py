"""Compile the SQL analyst graph and render its mermaid diagram to a PNG.

Usage:
    python tests/draw_graph.py
Produces: sql_analyst_graph.png in the project root.
"""

from agents.sql_analyst import build_sql_analyst_graph

if __name__ == "__main__":
    graph = build_sql_analyst_graph()
    png_bytes = graph.get_graph().draw_mermaid_png()
    out_path = "sql_analyst_graph.png"
    with open(out_path, "wb") as f:
        f.write(png_bytes)
    print(f"Wrote {out_path} ({len(png_bytes)} bytes)")
