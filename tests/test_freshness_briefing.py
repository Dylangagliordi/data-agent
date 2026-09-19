"""
Tests for utils/freshness_briefing.py (Spec 5: Freshness / Drift Briefing).

Requires a live Postgres. No LLM. Seeds real rows via the admin connection
and a real temporary folder/CSV file whose bytes are genuinely mutated
mid-test — same seed/restore discipline as test_dq_backlog.py, plus a real
file on disk rather than just a DB row.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_freshness_briefing.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.freshness_briefing import get_freshness_briefing
from utils.load_data import compute_file_checksum, get_admin_connection, write_data_quality_status

UNCHANGED_TABLE = "test_freshness_unchanged"
CHANGED_TABLE = "test_freshness_changed"
MISSING_FILE_TABLE = "test_freshness_missing_file"
NO_FOLDER_TABLE = "test_freshness_no_folder"


def test_full_freshness_lifecycle():
    conn = get_admin_connection()
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)

        unchanged_csv = tmp_path / f"{UNCHANGED_TABLE}.csv"
        unchanged_csv.write_text("a,b\n1,2\n")
        unchanged_checksum = compute_file_checksum(unchanged_csv)

        changed_csv = tmp_path / f"{CHANGED_TABLE}.csv"
        changed_csv.write_text("a,b\n1,2\n")
        original_changed_checksum = compute_file_checksum(changed_csv)

        # MISSING_FILE_TABLE deliberately gets no CSV written for it at all.

        try:
            write_data_quality_status(
                conn, UNCHANGED_TABLE, "pass", [], was_cleaned=True,
                source_folder=str(tmp_path), source_checksum=unchanged_checksum,
            )
            write_data_quality_status(
                conn, CHANGED_TABLE, "pass", [], was_cleaned=True,
                source_folder=str(tmp_path), source_checksum=original_changed_checksum,
            )
            write_data_quality_status(
                conn, MISSING_FILE_TABLE, "pass", [], was_cleaned=True,
                source_folder=str(tmp_path), source_checksum="deadbeef",
            )
            write_data_quality_status(
                conn, NO_FOLDER_TABLE, "pass", [], was_cleaned=True,
                source_folder=None, source_checksum=None,
            )

            # Genuinely mutate the "changed" file's real bytes before checking
            # — proving this detects real drift, not just echoing a flag.
            changed_csv.write_text("a,b\n1,2\n3,4\n")

            briefing = get_freshness_briefing()
            by_name = {entry["table_name"]: entry for entry in briefing}

            assert NO_FOLDER_TABLE not in by_name, (
                "a table with no recorded source_folder must never appear in the briefing"
            )

            assert by_name[UNCHANGED_TABLE]["changed"] is False
            assert by_name[UNCHANGED_TABLE]["source_file_found"] is True

            assert by_name[CHANGED_TABLE]["changed"] is True
            assert by_name[CHANGED_TABLE]["current_checksum"] != original_changed_checksum

            assert by_name[MISSING_FILE_TABLE]["source_file_found"] is False
            assert by_name[MISSING_FILE_TABLE]["changed"] is None, (
                "a table whose source file can't be found must report changed=None, "
                "never a fabricated False"
            )

            # Ranking: a changed table must rank before an unchanged one.
            names = [e["table_name"] for e in briefing]
            assert names.index(CHANGED_TABLE) < names.index(UNCHANGED_TABLE)

            print(
                "PASS: unchanged/changed/missing-file/no-folder cases all report "
                "honestly, ranked correctly"
            )
        finally:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM _data_quality_status WHERE table_name IN (%s, %s, %s, %s)",
                    (UNCHANGED_TABLE, CHANGED_TABLE, MISSING_FILE_TABLE, NO_FOLDER_TABLE),
                )
            conn.commit()
    conn.close()


if __name__ == "__main__":
    test_full_freshness_lifecycle()
    print("\nAll freshness_briefing tests passed.")
