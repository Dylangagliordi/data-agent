"""
Tests for Spec 10, Part 1: Format Normalization (utils/format_normalization.py).

Real, temporary JSON/Excel/HTML files (no mocking of pandas) — each
converted CSV is checked against real, hand-known content, not just "did a
file get written."

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_format_normalization.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from utils.format_normalization import largest_table, normalize_folder_to_csv, normalize_to_csv


def test_normalize_json_to_csv():
    with tempfile.TemporaryDirectory() as tmp_dir:
        json_path = Path(tmp_dir) / "data.json"
        pd.DataFrame({"id": [1, 2], "name": ["Alice", "Bob"]}).to_json(json_path, orient="records")

        csv_path = normalize_to_csv(json_path)
        assert csv_path == json_path.with_suffix(".csv")
        result = pd.read_csv(csv_path)
        assert list(result["name"]) == ["Alice", "Bob"]
        print("PASS: normalize_to_csv converts a real JSON file to a real, correct CSV")


def test_normalize_excel_to_csv():
    with tempfile.TemporaryDirectory() as tmp_dir:
        xlsx_path = Path(tmp_dir) / "data.xlsx"
        pd.DataFrame({"id": [1, 2, 3], "rating": [4.5, 3.0, 5.0]}).to_excel(xlsx_path, index=False)

        csv_path = normalize_to_csv(xlsx_path)
        assert csv_path == xlsx_path.with_suffix(".csv")
        result = pd.read_csv(csv_path)
        assert list(result["rating"]) == [4.5, 3.0, 5.0]
        print("PASS: normalize_to_csv converts a real Excel file to a real, correct CSV")


def test_normalize_html_picks_the_largest_table():
    with tempfile.TemporaryDirectory() as tmp_dir:
        html_path = Path(tmp_dir) / "page.html"
        # A real page with a tiny nav table AND the real, larger data table —
        # proves the "largest table" rule actually matters, not just "the only table."
        html_path.write_text(
            "<html><body>"
            "<table><tr><th>nav</th></tr><tr><td>home</td></tr></table>"
            "<table>"
            "<tr><th>industry</th><th>avg_rating</th></tr>"
            "<tr><td>Tech</td><td>4.5</td></tr>"
            "<tr><td>Retail</td><td>3.0</td></tr>"
            "<tr><td>Healthcare</td><td>4.0</td></tr>"
            "</table>"
            "</body></html>"
        )

        csv_path = normalize_to_csv(html_path)
        result = pd.read_csv(csv_path)
        assert "industry" in result.columns
        assert list(result["industry"]) == ["Tech", "Retail", "Healthcare"]
        print("PASS: normalize_to_csv extracts the real, larger data table, not the small nav table")


def test_csv_passthrough_is_a_true_noop():
    with tempfile.TemporaryDirectory() as tmp_dir:
        csv_path = Path(tmp_dir) / "already.csv"
        csv_path.write_text("id,name\n1,Alice\n")
        mtime_before = csv_path.stat().st_mtime

        result_path = normalize_to_csv(csv_path)
        assert result_path == csv_path
        assert csv_path.stat().st_mtime == mtime_before, "a .csv file must never be rewritten"
        print("PASS: a .csv file passes through completely unchanged")


def test_unsupported_extension_raises_clearly():
    with tempfile.TemporaryDirectory() as tmp_dir:
        bad_path = Path(tmp_dir) / "data.zip"
        bad_path.write_bytes(b"not a real zip")
        try:
            normalize_to_csv(bad_path)
            raise AssertionError("expected ValueError for an unsupported extension")
        except ValueError as e:
            assert "unsupported file extension" in str(e)
        print("PASS: an unsupported extension raises a clear ValueError, never silently produces a CSV")


def test_normalize_folder_to_csv_skips_existing_and_records_errors():
    with tempfile.TemporaryDirectory() as tmp_dir:
        folder = Path(tmp_dir)
        pd.DataFrame({"a": [1]}).to_json(folder / "one.json", orient="records")

        # This file already has a real, different .csv counterpart — must be
        # left alone, never overwritten with a freshly-converted version.
        pd.DataFrame({"a": [999]}).to_json(folder / "two.json", orient="records")
        (folder / "two.csv").write_text("a\nEXISTING\n")

        # An unrelated, unrecognized extension — must be silently skipped
        # entirely, never attempted, never an "error."
        (folder / "notes.txt").write_text("just some notes")

        # A file WITH a supported extension that still fails to parse — the
        # real "errors" case: attempted, genuinely fails, recorded honestly.
        (folder / "corrupt.json").write_text("{this is not valid json")

        outcome = normalize_folder_to_csv(folder)
        written_names = {p.name for p in outcome["written"]}
        assert written_names == {"one.csv"}, f"expected only one.csv to be newly written, got {written_names}"
        assert (folder / "two.csv").read_text() == "a\nEXISTING\n", "an existing .csv must never be overwritten"
        assert not (folder / "notes.txt.csv").exists() and not (folder / "notes.csv").exists(), (
            "an unrecognized extension must never be attempted at all"
        )
        assert len(outcome["errors"]) == 1 and "corrupt.json" in outcome["errors"][0]
        print("PASS: normalize_folder_to_csv converts only what needs it, never overwrites, ignores unrecognized files, and records real parse errors")


if __name__ == "__main__":
    test_normalize_json_to_csv()
    test_normalize_excel_to_csv()
    test_normalize_html_picks_the_largest_table()
    test_csv_passthrough_is_a_true_noop()
    test_unsupported_extension_raises_clearly()
    test_normalize_folder_to_csv_skips_existing_and_records_errors()
    print("\nAll format_normalization tests passed.")
