"""
Tests for utils/data_dictionary.py (Spec 2: Data Dictionary).

Requires a live Postgres with the real `uncleaned_ds_jobs` table already
loaded (same convention as test_fanout_status.py / test_transformation_options.py).
No LLM is ever called by this module.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_data_dictionary.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.data_dictionary import generate_data_dictionary, render_data_dictionary_html
from utils.db import get_app_reader_connection

REAL_TABLE = "uncleaned_ds_jobs"


def test_generate_data_dictionary_against_real_table():
    data = generate_data_dictionary(REAL_TABLE)
    assert data["table_name"] == REAL_TABLE

    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(f'SELECT COUNT(*) FROM "{REAL_TABLE}"')
            real_row_count = cur.fetchone()[0]
        conn.rollback()
    finally:
        conn.close()
    assert data["row_count"] == real_row_count > 0

    assert len(data["columns"]) > 0
    for column in data["columns"]:
        assert set(column) == {
            "name", "data_type", "role", "fanout_source", "has_fanout",
            "is_derived", "derived_kind", "high_cardinality_candidate",
        }
        assert column["role"] in ("regular", "primary key", "foreign key")
        if column["role"] == "regular":
            assert column["has_fanout"] is None
        else:
            assert isinstance(column["has_fanout"], bool)
    print(f"PASS: generate_data_dictionary matches real state for '{REAL_TABLE}' "
          f"({data['row_count']} rows, {len(data['columns'])} columns)")


def test_honest_absence_for_a_table_never_processed_by_the_pipeline():
    """_fanout_status itself is a real table that exists in Postgres but was
    never loaded through load_csv_to_table — it has no _data_quality_status
    row, no _fanout_status row ABOUT itself, no derived columns, and no
    transformation candidates. Every one of those must come back as an
    honest absence, never a fabricated default.
    """
    data = generate_data_dictionary("_fanout_status")
    assert data["quality_status"] is None
    assert data["row_count"] >= 0
    assert len(data["columns"]) > 0
    for column in data["columns"]:
        assert column["role"] == "regular"
        assert column["has_fanout"] is None
        assert column["is_derived"] is False
        assert column["derived_kind"] is None
        assert column["high_cardinality_candidate"] is False
    print("PASS: a table never processed by the pipeline reports honest absence, not fabricated defaults")


def test_render_data_dictionary_html_writes_real_file():
    path = render_data_dictionary_html(REAL_TABLE)
    assert os.path.isfile(path)
    content = open(path, encoding="utf-8").read()
    assert len(content) > 0
    assert f"<title>Data Dictionary: {REAL_TABLE}</title>" in content
    assert "Row count" in content
    print(f"PASS: render_data_dictionary_html wrote a real file at {path}")


if __name__ == "__main__":
    test_generate_data_dictionary_against_real_table()
    test_honest_absence_for_a_table_never_processed_by_the_pipeline()
    test_render_data_dictionary_html_writes_real_file()
    print("\nAll data_dictionary tests passed.")
