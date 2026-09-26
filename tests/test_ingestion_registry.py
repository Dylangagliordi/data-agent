"""
Tests for Spec 10, Part 3: Ingestion Source Registry
(utils/load_data.py's _ingestion_sources quartet, utils/ingestion_registry.py,
and its wiring into agents/etl_analyst.py:extract_load/scrape_load).

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_ingestion_registry.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agents.etl_analyst as etl_module
from agents.etl_analyst import extract_load
from utils.ingestion_registry import render_ingestion_sources_html
from utils.load_data import (
    ensure_ingestion_sources_table,
    get_admin_connection,
    read_ingestion_sources,
    record_ingestion_attempt,
)

TEST_URL = "https://example.com/_test_spec10_registry.csv"


def _cleanup(conn, *urls):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM _ingestion_sources WHERE url = ANY(%s)", (list(urls),))
    conn.commit()


def test_record_ingestion_attempt_upsert_semantics():
    conn = get_admin_connection()
    ensure_ingestion_sources_table(conn)
    _cleanup(conn, TEST_URL)
    try:
        record_ingestion_attempt(conn, TEST_URL, "/tmp/data", "download", "ok", "")
        rows = read_ingestion_sources(conn)
        row = next(r for r in rows if r["url"] == TEST_URL)
        assert row["fetch_count"] == 1
        assert row["status"] == "ok"
        first_fetched_at = row["first_fetched_at"]

        # A second, failing attempt against the same URL must UPDATE in
        # place, not insert a second row — fetch_count increments,
        # first_fetched_at is preserved, status/last_error reflect the
        # latest attempt.
        record_ingestion_attempt(conn, TEST_URL, "/tmp/data", "download", "error", "timed out")
        rows = read_ingestion_sources(conn)
        matches = [r for r in rows if r["url"] == TEST_URL]
        assert len(matches) == 1, "a repeated URL must update one row, never insert a second"
        row = matches[0]
        assert row["fetch_count"] == 2
        assert row["status"] == "error"
        assert row["last_error"] == "timed out"
        assert row["first_fetched_at"] == first_fetched_at, "first_fetched_at must never change on update"
        print("PASS: record_ingestion_attempt upserts correctly — one row per URL, real history preserved")
    finally:
        _cleanup(conn, TEST_URL)
        conn.close()


def test_extract_load_records_both_success_and_failure():
    conn = get_admin_connection()
    ensure_ingestion_sources_table(conn)

    success_url = "https://raw.githubusercontent.com/pandas-dev/pandas/main/README.md"
    metadata_url = "http://169.254.169.254/latest/meta-data/_test_spec10/"
    _cleanup(conn, success_url, metadata_url)
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            extract_load.invoke({"url": success_url, "output_folder": tmp_dir, "format": "md"})
            extract_load.invoke({"url": metadata_url, "output_folder": tmp_dir, "format": "csv"})

        rows = read_ingestion_sources(conn)
        success_row = next(r for r in rows if r["url"] == success_url)
        assert success_row["status"] == "ok"
        assert success_row["source_kind"] == "download"

        failed_row = next(r for r in rows if r["url"] == metadata_url)
        assert failed_row["status"] == "error"
        assert "private/internal/metadata" in failed_row["last_error"]
        print("PASS: extract_load records both a real successful fetch and a real SSRF-refused fetch")
    finally:
        _cleanup(conn, success_url, metadata_url)
        conn.close()


class _FakeResponse:
    def __init__(self, text: str, url: str):
        self.text = text
        self.content = text.encode("utf-8")
        self.url = url
        self.is_redirect = False
        self.is_permanent_redirect = False

    def raise_for_status(self):
        pass


def test_scrape_load_records_as_scrape_kind():
    conn = get_admin_connection()
    ensure_ingestion_sources_table(conn)
    url = "https://example.com/_test_spec10_scrape.html"
    _cleanup(conn, url)

    html = "<table><tr><th>a</th></tr><tr><td>1</td></tr></table>"
    original_get = etl_module.requests.get
    etl_module.requests.get = lambda *a, **k: _FakeResponse(html, url)
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            etl_module.scrape_load.invoke({"url": url, "output_folder": tmp_dir})
        rows = read_ingestion_sources(conn)
        row = next(r for r in rows if r["url"] == url)
        assert row["source_kind"] == "scrape"
        assert row["status"] == "ok"
        print("PASS: scrape_load records its own attempts with source_kind='scrape'")
    finally:
        etl_module.requests.get = original_get
        _cleanup(conn, url)
        conn.close()


def test_render_ingestion_sources_html():
    conn = get_admin_connection()
    ensure_ingestion_sources_table(conn)
    _cleanup(conn, TEST_URL)
    try:
        record_ingestion_attempt(conn, TEST_URL, "/tmp/data", "download", "error", "boom")
        path = render_ingestion_sources_html()
        content = Path(path).read_text()
        assert TEST_URL in content
        assert "boom" in content
        print(f"PASS: render_ingestion_sources_html renders real tracked sources: {path}")
    finally:
        _cleanup(conn, TEST_URL)
        conn.close()


if __name__ == "__main__":
    test_record_ingestion_attempt_upsert_semantics()
    test_extract_load_records_both_success_and_failure()
    test_scrape_load_records_as_scrape_kind()
    test_render_ingestion_sources_html()
    print("\nAll ingestion_registry tests passed.")
