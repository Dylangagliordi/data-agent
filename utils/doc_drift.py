"""Documentation Drift Detector (Spec 13, Part 1a): the real, live ground truth
this project's architecture — every graph node, every utils/ module, every CLI
command — so a hand-written reference document (the published architecture
artifact) can be checked against reality before being republished, instead of
silently drifting the way it already has once (it currently predates Specs 1
through 12 entirely).

This deliberately does NOT try to auto-rewrite or auto-publish the artifact
itself — publishing is a session-level action (the Artifact tool), and the
artifact's writing quality is a real editorial concern this module has no
business overriding. It only produces the one thing a rebuild needs to check
itself against: a live, mechanically-accurate inventory.

Public interface:
    get_real_inventory() -> dict
    render_inventory_html() -> str

CLI: `python main.py "inventory"`.
"""

import html
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = "inventory"

_UTILS_DIR = PROJECT_ROOT / "utils"


def _real_graph_nodes() -> dict:
    """Every real node name in all three compiled LangGraph graphs, via direct
    introspection of the actual compiled graph objects — never a hand-typed
    list that could itself go stale exactly like the artifact this exists to
    check. Building a graph never executes any node's body (same fact
    utils/system_map.py already relies on and tests), so this never opens a
    DB connection or calls an LLM."""
    from agents.data_agent import build_data_agent_graph
    from agents.etl_analyst import build_etl_analyst_graph
    from agents.sql_analyst import build_sql_analyst_graph

    graphs = {
        "data_agent": build_data_agent_graph(),
        "sql_analyst": build_sql_analyst_graph(),
        "etl_analyst": build_etl_analyst_graph(),
    }
    return {
        name: sorted(n for n in graph.get_graph().nodes.keys() if n not in ("__start__", "__end__"))
        for name, graph in graphs.items()
    }


def _real_utils_modules() -> list:
    """Every real utils/*.py module (excluding __init__ / private-looking
    scratch files), sorted."""
    if not _UTILS_DIR.exists():
        return []
    return sorted(
        p.stem for p in _UTILS_DIR.glob("*.py") if p.is_file() and not p.stem.startswith("_")
    )


def _real_cli_commands() -> list:
    """Every real CLI command main.py actually dispatches — read directly
    from utils.cli_modes.MODES (Spec 15, Part 1's real, structured mode
    registry). Before Spec 15 this regex-parsed main.py's own source text to
    reconstruct what should just be structured data — a fragile indirection
    this now eliminates entirely: MODES *is* the real, live ground truth,
    nothing here re-derives it from anything else."""
    from utils.cli_modes import MODES

    return sorted(mode.name for mode in MODES)


def get_real_inventory() -> dict:
    """The full, live ground truth: {"graph_nodes": {graph_name: [node, ...]},
    "utils_modules": [...], "cli_commands": [...]}. Every value is extracted
    directly from the real, current code — nothing here is a maintained list
    that could itself go stale."""
    return {
        "graph_nodes": _real_graph_nodes(),
        "utils_modules": _real_utils_modules(),
        "cli_commands": _real_cli_commands(),
    }


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def render_inventory_html() -> str:
    inventory = get_real_inventory()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    graph_sections = []
    for graph_name, nodes in inventory["graph_nodes"].items():
        items = "".join(f"<li><code>{_esc(n)}</code></li>" for n in nodes)
        graph_sections.append(f"<h3>{_esc(graph_name)} ({len(nodes)} nodes)</h3><ul>{items}</ul>")

    modules_html = "".join(f"<li><code>{_esc(m)}.py</code></li>" for m in inventory["utils_modules"])
    commands_html = "".join(f"<li><code>{_esc(c)}</code></li>" for c in inventory["cli_commands"])

    full_html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Code Inventory</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
h2 {{ border-bottom: 2px solid #ddd; padding-bottom: 6px; margin-top: 2rem; }}
h3 {{ margin-top: 1.2rem; }}
ul {{ columns: 2; }}
code {{ background: #f2f5f9; padding: 1px 5px; border-radius: 3px; }}
.meta {{ color: #666; font-size: 0.88em; }}
</style>
</head>
<body>
<h1>Code Inventory</h1>
<p class="meta">Generated {generated_at} — the real, live ground truth this project's code
actually contains right now. Cross-check any rebuild of the architecture artifact against
this page before publishing it.</p>
<h2>Graph nodes</h2>
{"".join(graph_sections)}
<h2>utils/ modules ({len(inventory['utils_modules'])})</h2>
<ul>{modules_html}</ul>
<h2>CLI commands ({len(inventory['cli_commands'])})</h2>
<ul>{commands_html}</ul>
</body>
</html>
"""
    out_dir = Path(OUTPUT_DIR)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"inventory_{timestamp}.html"
    out_path.write_text(full_html, encoding="utf-8")
    return str(out_path)
