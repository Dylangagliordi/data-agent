"""
Tests for Spec 7: Cleaning Recipe Cache (utils/data_cleaning.py's recipe_conn
plumbing + utils/load_data.py's _cleaning_recipes table).

Requires a live Postgres (admin connection, same as test_transformation_options.py)
for the recipe table itself. Uses a fake, call-counting LLM — never a live model —
and real temporary CSV files/folders, with approval auto-piped via a stdin
redirect (same discipline as test_composite_field_split.py's redirect_stdin_yes).

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_cleaning_recipe_cache.py
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

from utils.data_cleaning import (
    _issue_treatment_signature,
    _read_csv_robust,
    check_rubric,
    clean_dataset,
    compute_recipe_id,
)
from utils.load_data import (
    ensure_cleaning_recipes_table,
    get_admin_connection,
    read_cleaning_recipe,
    sanitize_identifier,
    write_cleaning_recipe,
)

_PATH_RE = re.compile(r"File to clean \(read and overwrite this exact path\): (.+)")


class FakeCleaningLLM:
    """Only implements plain .invoke() (no .with_structured_output) — discovery
    (explore_column/_verify_hypothesis/_explore_column_pairs) catches the
    resulting AttributeError and treats it as "nothing noticed", the same
    documented convention every other fake LLM in this test suite relies on.
    Always generates a real, correct fix: replace '-1' with a real null in the
    'rating' column of whatever path it's told to clean.
    """

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
        "6,Frank,-1\n"
        "7,Grace,4.1\n"
        "8,Heidi,3.2\n"
        "9,Ivan,3.7\n"
        "10,Judy,4.5\n"
    )


def _rating_still_has_placeholder(csv_path: Path) -> bool:
    return "-1" in Path(csv_path).read_text().split("\n")[2]  # cheap real-content check


def test_cache_miss_then_cache_hit_zero_llm_calls():
    admin_conn = get_admin_connection()
    ensure_cleaning_recipes_table(admin_conn)
    fake = FakeCleaningLLM()

    with tempfile.TemporaryDirectory() as tmp_dir:
        folder = Path(tmp_dir) / "orders"
        folder.mkdir()
        raw_csv = folder / "orders.csv"
        _write_fixture_csv(raw_csv)
        table_name = sanitize_identifier(raw_csv.stem)

        try:
            # Run 1: cache miss — real generate/approve/execute cycle.
            with _redirect_stdin_yes():
                clean_dataset(folder, llm=fake, recipe_conn=admin_conn)
            calls_after_run_1 = fake.call_count
            assert calls_after_run_1 > 0, "first run must actually call the LLM (cache miss)"

            cleaned_csv = folder / "cleaned" / "orders.csv"
            assert "-1" not in cleaned_csv.read_text(), "the real fix must have actually run"

            issues = check_rubric(raw_csv)
            placeholder_issue = next(i for i in issues if i.startswith("Placeholder values:"))
            signature = _issue_treatment_signature(placeholder_issue, df=_read_csv_robust(raw_csv))
            recipe_id = compute_recipe_id(table_name, signature)
            saved = read_cleaning_recipe(admin_conn, table_name, recipe_id)
            assert saved is not None, "a resolved, signature-eligible fix must be saved as a recipe"

            # Simulate a fresh reload: delete the cleaned/ clone so clean_dataset
            # has to re-clone and re-process from the raw file, exactly like a
            # real second-week run would.
            cleaned_csv.unlink()

            # Run 2: same table, same signature — must be a cache hit.
            with _redirect_stdin_yes():
                clean_dataset(folder, llm=fake, recipe_conn=admin_conn)
            assert fake.call_count == calls_after_run_1, (
                "a cache hit must make zero new LLM calls"
            )
            assert "-1" not in cleaned_csv.read_text(), (
                "a replayed recipe must still be re-verified for real, not just trusted blindly"
            )
            print("PASS: cache miss generates+saves a recipe; cache hit replays it with zero LLM calls")
        finally:
            with admin_conn.cursor() as cur:
                cur.execute("DELETE FROM _cleaning_recipes WHERE table_name = %s", (table_name,))
            admin_conn.commit()
    admin_conn.close()


def test_same_signature_different_table_is_not_reused():
    admin_conn = get_admin_connection()
    ensure_cleaning_recipes_table(admin_conn)
    fake = FakeCleaningLLM()

    with tempfile.TemporaryDirectory() as tmp_dir:
        folder_a = Path(tmp_dir) / "orders_a"
        folder_a.mkdir()
        csv_a = folder_a / "orders_a.csv"
        _write_fixture_csv(csv_a)
        table_a = sanitize_identifier(csv_a.stem)

        folder_b = Path(tmp_dir) / "orders_b"
        folder_b.mkdir()
        csv_b = folder_b / "orders_b.csv"
        _write_fixture_csv(csv_b)  # identical shape/content -> identical signature
        table_b = sanitize_identifier(csv_b.stem)

        try:
            with _redirect_stdin_yes():
                clean_dataset(folder_a, llm=fake, recipe_conn=admin_conn)
            calls_after_a = fake.call_count
            assert calls_after_a > 0

            with _redirect_stdin_yes():
                clean_dataset(folder_b, llm=fake, recipe_conn=admin_conn)
            assert fake.call_count > calls_after_a, (
                "an identical signature on a DIFFERENT table must never reuse the first "
                "table's recipe — same cross-table mixup class of bug this project has "
                "already hit once with signature-only matching"
            )
            print("PASS: an identical issue signature on a different table triggers a fresh generation")
        finally:
            with admin_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM _cleaning_recipes WHERE table_name IN (%s, %s)", (table_a, table_b)
                )
            admin_conn.commit()
    admin_conn.close()


def test_recipe_conn_none_never_touches_the_database():
    import utils.load_data as load_data_module

    def _boom(*args, **kwargs):
        raise AssertionError("recipe_conn=None must never touch _cleaning_recipes at all")

    original_read = load_data_module.read_cleaning_recipe
    original_write = load_data_module.write_cleaning_recipe
    load_data_module.read_cleaning_recipe = _boom
    load_data_module.write_cleaning_recipe = _boom
    try:
        fake = FakeCleaningLLM()
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder = Path(tmp_dir) / "orders"
            folder.mkdir()
            _write_fixture_csv(folder / "orders.csv")
            with _redirect_stdin_yes():
                clean_dataset(folder, llm=fake)  # recipe_conn defaults to None
        print("PASS: recipe_conn=None (clean_data.py's standalone usage) never touches the recipe table")
    finally:
        load_data_module.read_cleaning_recipe = original_read
        load_data_module.write_cleaning_recipe = original_write


def test_a_replayed_recipe_that_fails_verification_falls_back_to_fresh_generation():
    admin_conn = get_admin_connection()
    ensure_cleaning_recipes_table(admin_conn)
    fake = FakeCleaningLLM()

    with tempfile.TemporaryDirectory() as tmp_dir:
        folder = Path(tmp_dir) / "orders"
        folder.mkdir()
        raw_csv = folder / "orders.csv"
        _write_fixture_csv(raw_csv)
        table_name = sanitize_identifier(raw_csv.stem)

        issues = check_rubric(raw_csv)
        placeholder_issue = next(i for i in issues if i.startswith("Placeholder values:"))
        signature = _issue_treatment_signature(placeholder_issue, df=_read_csv_robust(raw_csv))
        recipe_id = compute_recipe_id(table_name, signature)

        # Seed a bogus "recipe" that runs without error but does NOT actually
        # fix anything — a no-op script — to prove a broken replay is discarded
        # rather than trusted.
        from utils.data_cleaning import _RECIPE_PATH_PLACEHOLDER

        bogus_code = (
            "import pandas as pd\n"
            f"path = {_RECIPE_PATH_PLACEHOLDER!r}\n"
            "df = pd.read_csv(path, dtype=str)\n"
            "df.to_csv(path, index=False)\n"  # does nothing to the '-1' values
        )
        write_cleaning_recipe(admin_conn, table_name, recipe_id, signature, bogus_code)

        try:
            with _redirect_stdin_yes():
                clean_dataset(folder, llm=fake, recipe_conn=admin_conn)
            assert fake.call_count > 0, (
                "a cached recipe that doesn't actually resolve the issue must fall back "
                "to a real, fresh LLM generation, never be accepted as resolved"
            )
            cleaned_csv = folder / "cleaned" / "orders.csv"
            assert "-1" not in cleaned_csv.read_text(), (
                "the fallback fresh generation must still actually fix the file"
            )
            print("PASS: a replayed recipe that fails verification is discarded and falls back to fresh generation")
        finally:
            with admin_conn.cursor() as cur:
                cur.execute("DELETE FROM _cleaning_recipes WHERE table_name = %s", (table_name,))
            admin_conn.commit()
    admin_conn.close()


if __name__ == "__main__":
    test_cache_miss_then_cache_hit_zero_llm_calls()
    test_same_signature_different_table_is_not_reused()
    test_recipe_conn_none_never_touches_the_database()
    test_a_replayed_recipe_that_fails_verification_falls_back_to_fresh_generation()
    print("\nAll cleaning_recipe_cache tests passed.")
