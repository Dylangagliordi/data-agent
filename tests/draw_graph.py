"""Regenerate sql_analyst_graph.png from the real, current graph.

Usage:
    python tests/draw_graph.py
Produces: sql_analyst_graph.png in the project root.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.system_map import generate_system_map

if __name__ == "__main__":
    png_bytes = generate_system_map("sql_analyst")
    out_path = "sql_analyst_graph.png"
    with open(out_path, "wb") as f:
        f.write(png_bytes)
    print(f"Wrote {out_path} ({len(png_bytes)} bytes)")
