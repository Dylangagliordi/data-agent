"""
Tests for Spec 10, Part 1: format normalization wired into transform_load
(agents/etl_analyst.py).

Same monkeypatch-utils.llm_pick.pick_llm technique
tests/test_transform_load_recipe_cache.py already uses (transform_load has no
llm= injection point by design) — a fake LLM with no .with_structured_output
so discovery's AttributeError-catch treats it as "nothing noticed," the
documented convention every fake LLM in this suite relies on. Requires live
Postgres for the recipe table transform_load always touches.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_transform_load_format_normalization.py
"""

import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import utils.llm_pick as llm_pick_module
from agents.etl_analyst import transform_load
from utils.load_data import get_admin_connection, sanitize_identifier


class FakeNoOpLLM:
    """Only implements plain .invoke() (no .with_structured_output) — every
    discovery call catches the resulting AttributeError as "nothing noticed."
    Never actually reached for a genuinely clean file, but present so this
    test never makes a real, billed API call regardless."""

    def invoke(self, messages):
        return SimpleNamespace(content="nothing to report")


@contextlib.contextmanager
def _redirect_stdin_yes():
    original_stdin = sys.stdin
    sys.stdin = io.StringIO("yes\n" * 20)
    try:
        yield
    finally:
        sys.stdin = original_stdin


def test_transform_load_normalizes_json_before_cleaning():
    fake = FakeNoOpLLM()
    original_pick_llm = llm_pick_module.pick_llm
    llm_pick_module.pick_llm = lambda level: fake

    admin_conn = get_admin_connection()
    with tempfile.TemporaryDirectory() as tmp_dir:
        folder = Path(tmp_dir) / "scraped"
        folder.mkdir()
        json_path = folder / "table.json"
        pd.DataFrame(
            {"id": [1, 2, 3], "name": ["Alice", "Bob", "Carol"]}
        ).to_json(json_path, orient="records")
        table_name = sanitize_identifier(json_path.stem)

        try:
            with _redirect_stdin_yes():
                result = transform_load.invoke({"folder_path": str(folder)})

            csv_path = folder / "table.csv"
            assert csv_path.exists(), "transform_load must normalize the JSON file to CSV before cleaning"
            written = pd.read_csv(csv_path)
            assert list(written["name"]) == ["Alice", "Bob", "Carol"]
            assert "table.csv" in result or "table.json" in result

            print("PASS: transform_load normalizes a real JSON file to CSV before running clean_dataset")
        finally:
            with admin_conn.cursor() as cur:
                cur.execute("DELETE FROM _cleaning_recipes WHERE table_name = %s", (table_name,))
            admin_conn.commit()
            admin_conn.close()
            llm_pick_module.pick_llm = original_pick_llm


if __name__ == "__main__":
    test_transform_load_normalizes_json_before_cleaning()
    print("\nAll transform_load_format_normalization tests passed.")
