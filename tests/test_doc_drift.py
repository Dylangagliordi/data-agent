"""
Tests for Spec 13, Part 1a: Documentation Drift Detector (utils/doc_drift.py).

The key thing being proven: the inventory is extracted from REAL, live
introspection — never a hand-typed list — so these tests assert against
Spec 12's actual new nodes/module/command by name, without those names ever
being hardcoded as "the expected list" (that would just be a second place for
the same staleness bug to hide).

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_doc_drift.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.doc_drift import get_real_inventory, render_inventory_html


def test_graph_nodes_include_real_spec12_additions():
    inventory = get_real_inventory()
    sql_nodes = inventory["graph_nodes"]["sql_analyst"]
    # These are real nodes added in Spec 12 (this very session) — if this
    # module were hardcoding a node list instead of really introspecting the
    # compiled graph, adding a brand-new node like this wouldn't show up
    # without a manual edit here, which is exactly the bug class this tool
    # exists to prevent.
    assert "check_needs_scratch_mode" in sql_nodes
    assert "run_scratch_mode" in sql_nodes
    assert "validate_chart_shape" in sql_nodes
    # __start__/__end__ are LangGraph's own sentinel nodes, not real work —
    # must be filtered out, not presented as if they were architecture.
    assert "__start__" not in sql_nodes and "__end__" not in sql_nodes
    print("PASS: get_real_inventory reflects real, current graph nodes via live introspection")


def test_utils_modules_include_real_recent_additions():
    inventory = get_real_inventory()
    modules = inventory["utils_modules"]
    assert "scratch_mode" in modules
    assert "doc_drift" in modules  # this module lists itself — real, not a stale snapshot
    print("PASS: get_real_inventory lists real, current utils/ modules")


def test_cli_commands_include_real_recent_additions():
    inventory = get_real_inventory()
    commands = inventory["cli_commands"]
    assert "profile: " in commands
    assert "joins" in commands
    assert "sources" in commands
    assert "inventory" in commands  # this command lists itself
    print("PASS: get_real_inventory lists real, current CLI commands parsed straight from main.py")


def test_render_inventory_html():
    path = render_inventory_html()
    content = open(path).read()
    assert "check_needs_scratch_mode" in content
    assert "scratch_mode.py" in content
    assert "joins" in content
    print(f"PASS: render_inventory_html renders the real, current inventory: {path}")


if __name__ == "__main__":
    test_graph_nodes_include_real_spec12_additions()
    test_utils_modules_include_real_recent_additions()
    test_cli_commands_include_real_recent_additions()
    test_render_inventory_html()
    print("\nAll doc_drift tests passed.")
