"""
Tests for Spec 7b: the recipe cache wired into the ETL analyst's transform_load
tool (agents/etl_analyst.py). Requires live Postgres for the recipe table.

transform_load has no llm= injection point by design (its whole point is
"always real, no exceptions" — see its own docstring), so this monkeypatches
utils.llm_pick.pick_llm itself, the same technique
tests/test_min_sample_rule_compliance.py already uses for exactly this
situation — never a live model call.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_transform_load_recipe_cache.py
"""

import contextlib
import io
import os
import re
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.llm_pick as llm_pick_module
from agents.etl_analyst import transform_load
from utils.load_data import get_admin_connection, sanitize_identifier

_PATH_RE = re.compile(r"File to clean \(read and overwrite this exact path\): (.+)")


class FakeCleaningLLM:
    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        human_content = messages[1][1]
        path = _PATH_RE.search(human_content).group(1).strip()
        code = (
            "import pandas as pd\n"
            f"path = {path!r}\n"
            "df = pd.read_csv(path, dtype=str)\n"
            "df['rating'] = df['rating'].replace('-1', pd.NA)\n"
            "df.to_csv(path, index=False)\n"
        )
        return SimpleNamespace(content=code)


@contextlib.contextmanager
def _redirect_stdin_yes():
    original_stdin = sys.stdin
    sys.stdin = io.StringIO("yes\n" * 20)
    try:
        yield
    finally:
        sys.stdin = original_stdin


def _write_fixture_csv(path: Path):
    path.write_text(
        "id,name,rating\n"
        "1,Alice,3.5\n"
        "2,Bob,-1\n"
        "3,Carol,4.0\n"
        "4,Dave,2.8\n"
        "5,Erin,3.9\n"
    )


def test_transform_load_replays_an_approved_recipe_with_zero_new_llm_calls():
    fake = FakeCleaningLLM()
    original_pick_llm = llm_pick_module.pick_llm
    llm_pick_module.pick_llm = lambda level: fake

    admin_conn = get_admin_connection()
    with tempfile.TemporaryDirectory() as tmp_dir:
        folder = Path(tmp_dir) / "orders"
        folder.mkdir()
        raw_csv = folder / "orders.csv"
        _write_fixture_csv(raw_csv)
        table_name = sanitize_identifier(raw_csv.stem)

        try:
            with _redirect_stdin_yes():
                result_1 = transform_load.invoke({"folder_path": str(folder)})
            calls_after_run_1 = fake.call_count
            assert calls_after_run_1 > 0, "first call must actually reach the LLM"
            assert "orders.csv" in result_1

            cleaned_csv = folder / "cleaned" / "orders.csv"
            assert "-1" not in cleaned_csv.read_text()
            cleaned_csv.unlink()  # simulate a fresh reload of the same recurring dataset

            with _redirect_stdin_yes():
                transform_load.invoke({"folder_path": str(folder)})
            assert fake.call_count == calls_after_run_1, (
                "transform_load must replay the already-approved recipe on the "
                "second ask, exactly like the other two Spec 7 entry points"
            )
            assert "-1" not in cleaned_csv.read_text()
            print(
                "PASS: transform_load reuses an approved recipe on a recurring "
                "dataset with zero new LLM calls"
            )
        finally:
            with admin_conn.cursor() as cur:
                cur.execute("DELETE FROM _cleaning_recipes WHERE table_name = %s", (table_name,))
            admin_conn.commit()
            admin_conn.close()
            llm_pick_module.pick_llm = original_pick_llm


if __name__ == "__main__":
    test_transform_load_replays_an_approved_recipe_with_zero_new_llm_calls()
    print("\nAll transform_load_recipe_cache tests passed.")
