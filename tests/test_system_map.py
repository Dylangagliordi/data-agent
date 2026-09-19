"""
Tests for utils/system_map.py (Spec 1: Live System Self-Map).

No DB, no LLM, no live network — building and introspecting a compiled
LangGraph never executes any node's body, so none of these should need
credentials or a running Postgres instance.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_system_map.py
"""

import os
import sys
import tempfile

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.system_map import generate_all_system_maps, generate_system_map

GRAPH_NAMES = ("sql_analyst", "etl_analyst", "data_agent")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_generate_system_map_returns_real_png_bytes():
    for graph_name in GRAPH_NAMES:
        png_bytes = generate_system_map(graph_name)
        assert isinstance(png_bytes, bytes)
        assert len(png_bytes) > 0
        assert png_bytes.startswith(PNG_MAGIC), (
            f"{graph_name}: expected real PNG bytes, got {png_bytes[:16]!r}"
        )
    print("PASS: generate_system_map returns real, non-empty PNG bytes for all three graphs")


def test_invalid_graph_name_raises_value_error():
    try:
        generate_system_map("not_a_real_graph")
    except ValueError as e:
        message = str(e)
        for graph_name in GRAPH_NAMES:
            assert graph_name in message, f"expected {graph_name!r} named in the error message"
    else:
        raise AssertionError("expected ValueError for an unknown graph_name")
    print("PASS: an invalid graph_name raises ValueError naming the three valid options")


def test_generate_all_system_maps_writes_three_files():
    with tempfile.TemporaryDirectory() as tmp_dir:
        written = generate_all_system_maps(tmp_dir)
        assert set(written) == set(GRAPH_NAMES)
        for graph_name, path in written.items():
            assert os.path.isfile(path), f"{graph_name}: expected a file at {path}"
            assert os.path.getsize(path) > 0
            assert path.endswith(f"{graph_name}_graph.png")
    print("PASS: generate_all_system_maps writes exactly three real, non-empty PNG files")


def test_no_db_or_llm_call_is_ever_made():
    """Compiling and introspecting a graph must never open a database connection
    or call an LLM — same discipline as test_is_safe.py proving the AST safety
    gate never consults the LLM judge.
    """
    import utils.db as db_module
    import utils.llm_pick as llm_pick_module

    def _boom(*args, **kwargs):
        raise AssertionError("system_map must never open a DB connection or call an LLM")

    original_get_conn = db_module.get_app_reader_connection
    original_pick_llm = llm_pick_module.pick_llm
    db_module.get_app_reader_connection = _boom
    llm_pick_module.pick_llm = _boom
    try:
        for graph_name in GRAPH_NAMES:
            generate_system_map(graph_name)
    finally:
        db_module.get_app_reader_connection = original_get_conn
        llm_pick_module.pick_llm = original_pick_llm
    print("PASS: generate_system_map never opens a DB connection or calls an LLM")


if __name__ == "__main__":
    test_generate_system_map_returns_real_png_bytes()
    test_invalid_graph_name_raises_value_error()
    test_generate_all_system_maps_writes_three_files()
    test_no_db_or_llm_call_is_ever_made()
    print("\nAll system_map tests passed.")
