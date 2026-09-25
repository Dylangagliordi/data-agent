"""
Tests for Spec 8, Part 2: Taxonomy Governance (utils/taxonomy_governance.py).

Pure file-based — no DB, no LLM. Monkeypatches the module's
REFERENCE_MAPPINGS_DIR/OUTPUT_DIR constants to isolated tmp directories so
this never touches the project's real utils/reference_mappings/.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_taxonomy_governance.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.taxonomy_governance as tg


def _write_version(dir_path: Path, base_name: str, version: int, column: str, mapping: dict, source: str):
    payload = {
        "column": column,
        "version": version,
        "declared_category_count": len(set(mapping.values())),
        "reference_source": source,
        "provenance": {},
        "created_at": f"2026-0{version}-01T00:00:00+00:00",
        "mapping": mapping,
    }
    (dir_path / f"{base_name}_v{version}.json").write_text(json.dumps(payload))


def test_list_reference_mappings_empty():
    with tempfile.TemporaryDirectory() as tmp_dir:
        original = tg.REFERENCE_MAPPINGS_DIR
        tg.REFERENCE_MAPPINGS_DIR = Path(tmp_dir) / "nonexistent"
        try:
            assert tg.list_reference_mappings() == []
            print("PASS: list_reference_mappings returns [] with no directory/files")
        finally:
            tg.REFERENCE_MAPPINGS_DIR = original


def test_list_and_diff_mapping_versions():
    with tempfile.TemporaryDirectory() as tmp_dir:
        dir_path = Path(tmp_dir)
        _write_version(
            dir_path, "industry_categories", 1, "Industry",
            {"Biotech & Pharmaceuticals": "Healthcare", "IT Services": "Technology"},
            "manual entry v1",
        )
        _write_version(
            dir_path, "industry_categories", 2, "Industry",
            {"Biotech & Pharmaceuticals": "Biotech", "IT Services": "Technology", "Aerospace": "Manufacturing"},
            "manual entry v2",
        )

        original = tg.REFERENCE_MAPPINGS_DIR
        tg.REFERENCE_MAPPINGS_DIR = dir_path
        try:
            groups = tg.list_reference_mappings()
            assert len(groups) == 1
            group = groups[0]
            assert group["base_name"] == "industry_categories"
            assert [p["version"] for p in group["versions"]] == [1, 2]

            diff = tg.diff_mapping_versions(group["versions"][0], group["versions"][1])
            assert diff["added"] == ["Aerospace"]
            assert diff["removed"] == []
            assert diff["changed"] == [("Biotech & Pharmaceuticals", "Healthcare", "Biotech")]
            print("PASS: list_reference_mappings groups by base_name; diff_mapping_versions detects real changes")
        finally:
            tg.REFERENCE_MAPPINGS_DIR = original


def test_render_taxonomy_governance_html():
    with tempfile.TemporaryDirectory() as tmp_dir:
        dir_path = Path(tmp_dir) / "mappings"
        dir_path.mkdir()
        out_dir = Path(tmp_dir) / "out"
        _write_version(
            dir_path, "industry_categories", 1, "Industry",
            {"IT Services": "Technology"}, "manual entry",
        )

        original_dir, original_out = tg.REFERENCE_MAPPINGS_DIR, tg.OUTPUT_DIR
        tg.REFERENCE_MAPPINGS_DIR = dir_path
        tg.OUTPUT_DIR = out_dir
        try:
            path = tg.render_taxonomy_governance_html()
            content = Path(path).read_text()
            assert "Industry" in content
            assert "IT Services" in content
            assert "Technology" in content
            assert "nothing to diff against" in content
            print("PASS: render_taxonomy_governance_html renders a real single-version group")
        finally:
            tg.REFERENCE_MAPPINGS_DIR, tg.OUTPUT_DIR = original_dir, original_out


def test_render_taxonomy_governance_html_empty():
    with tempfile.TemporaryDirectory() as tmp_dir:
        original_dir, original_out = tg.REFERENCE_MAPPINGS_DIR, tg.OUTPUT_DIR
        tg.REFERENCE_MAPPINGS_DIR = Path(tmp_dir) / "nonexistent"
        tg.OUTPUT_DIR = Path(tmp_dir) / "out"
        try:
            path = tg.render_taxonomy_governance_html()
            content = Path(path).read_text()
            assert "No reference mappings have been saved" in content
            print("PASS: render_taxonomy_governance_html renders an honest empty page")
        finally:
            tg.REFERENCE_MAPPINGS_DIR, tg.OUTPUT_DIR = original_dir, original_out


if __name__ == "__main__":
    test_list_reference_mappings_empty()
    test_list_and_diff_mapping_versions()
    test_render_taxonomy_governance_html()
    test_render_taxonomy_governance_html_empty()
    print("\nAll taxonomy_governance tests passed.")
