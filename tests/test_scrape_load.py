"""
Tests for Spec 10, Part 2: Scraping (agents/etl_analyst.py:scrape_load).

Two kinds of test:
1. SSRF protection — proves scrape_load refuses a metadata-endpoint URL
   before any request, via the exact same _fetch_with_ssrf_protection
   extract_load already uses and is already regression-tested in
   tests/test_extract_load_ssrf.py.
2. Real HTML-table extraction — monkeypatches only requests.get's response
   body (never _validate_fetch_url), so the real, safety-critical SSRF
   validation still runs against a real hostname on every test, while table
   parsing is checked deterministically against hand-written HTML rather
   than depending on some live page's actual, changeable content.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_scrape_load.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import agents.etl_analyst as etl_module
from agents.etl_analyst import scrape_load


def test_scrape_load_refuses_metadata_endpoint_before_any_request():
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_dir = Path(tmp_dir) / "out"
        result = scrape_load.invoke(
            {"url": "http://169.254.169.254/latest/meta-data/", "output_folder": str(out_dir)}
        )
        assert result.startswith("ERROR:")
        assert "private/internal/metadata" in result
        assert not out_dir.exists(), "no output folder should ever be created for a refused URL"
        print("PASS: scrape_load refuses a metadata-endpoint URL before any request or file I/O")


class _FakeResponse:
    def __init__(self, text: str, url: str):
        self.text = text
        self.content = text.encode("utf-8")
        self.url = url
        self.is_redirect = False
        self.is_permanent_redirect = False

    def raise_for_status(self):
        pass


def test_scrape_load_extracts_the_largest_real_table():
    html = (
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
    url = "https://example.com/data/industries.html"
    original_get = etl_module.requests.get
    etl_module.requests.get = lambda *a, **k: _FakeResponse(html, url)

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_dir = Path(tmp_dir) / "scraped"
        try:
            result = scrape_load.invoke({"url": url, "output_folder": str(out_dir)})
            assert "Scraped" in result and "3 rows" in result and "2 columns" in result

            csv_path = out_dir / "industries.csv"
            assert csv_path.exists(), f"expected {csv_path} to exist, dir has: {list(out_dir.iterdir())}"
            df = pd.read_csv(csv_path)
            assert list(df["industry"]) == ["Tech", "Retail", "Healthcare"], (
                "must extract the real, larger data table, not the small nav table"
            )
        finally:
            etl_module.requests.get = original_get
    print("PASS: scrape_load fetches a real page and extracts the real, larger data table as CSV")


def test_scrape_load_no_table_found():
    html = "<html><body><p>No tables here.</p></body></html>"
    url = "https://example.com/empty.html"
    original_get = etl_module.requests.get
    etl_module.requests.get = lambda *a, **k: _FakeResponse(html, url)

    with tempfile.TemporaryDirectory() as tmp_dir:
        out_dir = Path(tmp_dir) / "scraped"
        try:
            result = scrape_load.invoke({"url": url, "output_folder": str(out_dir)})
            assert result.startswith("ERROR:") and "no table found" in result
        finally:
            etl_module.requests.get = original_get
    print("PASS: scrape_load returns a clear error, never raises, when the page has no real table")


if __name__ == "__main__":
    test_scrape_load_refuses_metadata_endpoint_before_any_request()
    test_scrape_load_extracts_the_largest_real_table()
    test_scrape_load_no_table_found()
    print("\nAll scrape_load tests passed.")
