"""Regenerate data_agent_graph.png from the real, current graph.

Usage:
    python tests/draw_data_agent_graph.py
Produces: data_agent_graph.png in the project root.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.system_map import generate_system_map

if __name__ == "__main__":
    png_bytes = generate_system_map("data_agent")
    out_path = "data_agent_graph.png"
    with open(out_path, "wb") as f:
        f.write(png_bytes)
    print(f"Wrote {out_path} ({len(png_bytes)} bytes)")
