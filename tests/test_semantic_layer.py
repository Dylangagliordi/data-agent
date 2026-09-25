"""
Tests for Spec 8, Part 1: Semantic Layer (utils/semantic_layer.py +
utils/load_data.py's _saved_metrics quartet).

The DB round-trip test requires live Postgres (admin connection, same as
every other internal-table test in this suite).

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_semantic_layer.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.load_data import (
    delete_saved_metric,
    ensure_saved_metrics_table,
    get_admin_connection,
    read_saved_metrics,
    write_saved_metric,
)
from utils.semantic_layer import parse_define_metric_command, render_semantic_layer_html


def test_parse_define_metric_command_basic():
    parsed = parse_define_metric_command(
        "active_customers = COUNT(DISTINCT customer_id) FILTER (WHERE last_order > now() - interval '90 days')"
    )
    assert parsed["metric_name"] == "active_customers"
    assert parsed["sql_fragment"] == (
        "COUNT(DISTINCT customer_id) FILTER (WHERE last_order > now() - interval '90 days')"
    )
    assert parsed["description"] == ""
    print("PASS: parse_define_metric_command with no description")


def test_parse_define_metric_command_with_description():
    parsed = parse_define_metric_command(
        "avg_rating = AVG(rating) -- the canonical satisfaction metric, never a raw mean of nulls"
    )
    assert parsed["metric_name"] == "avg_rating"
    assert parsed["sql_fragment"] == "AVG(rating)"
    assert parsed["description"] == "the canonical satisfaction metric, never a raw mean of nulls"
    print("PASS: parse_define_metric_command with a description")


def test_parse_define_metric_command_rejects_bad_input():
    for bad in ["no equals sign here", "1bad_name = AVG(x)", "ok_name = "]:
        try:
            parse_define_metric_command(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass
    print("PASS: parse_define_metric_command rejects malformed input")


def test_render_semantic_layer_html_empty_and_populated():
    empty_path = render_semantic_layer_html([])
    assert "No metrics defined yet" in open(empty_path).read()

    fake_metrics = [
        {
            "metric_name": "avg_rating",
            "sql_fragment": "AVG(rating)",
            "description": "canonical satisfaction metric",
            "updated_at": "2026-01-01T00:00:00+00:00",
        }
    ]
    populated_path = render_semantic_layer_html(fake_metrics)
    content = open(populated_path).read()
    assert "avg_rating" in content
    assert "AVG(rating)" in content
    assert "canonical satisfaction metric" in content
    print("PASS: render_semantic_layer_html renders both the empty and populated cases")


def test_saved_metrics_db_round_trip():
    conn = get_admin_connection()
    ensure_saved_metrics_table(conn)
    metric_name = "_test_spec8_metric"
    try:
        write_saved_metric(conn, metric_name, "COUNT(*)", "test description")
        metrics = read_saved_metrics(conn)
        matches = [m for m in metrics if m["metric_name"] == metric_name]
        assert len(matches) == 1
        assert matches[0]["sql_fragment"] == "COUNT(*)"
        assert matches[0]["description"] == "test description"

        # Upsert: redefining the same name updates in place, not a duplicate row.
        write_saved_metric(conn, metric_name, "COUNT(DISTINCT id)", "updated description")
        metrics = read_saved_metrics(conn)
        matches = [m for m in metrics if m["metric_name"] == metric_name]
        assert len(matches) == 1
        assert matches[0]["sql_fragment"] == "COUNT(DISTINCT id)"

        deleted = delete_saved_metric(conn, metric_name)
        assert deleted is True
        metrics = read_saved_metrics(conn)
        assert not any(m["metric_name"] == metric_name for m in metrics)
        print("PASS: _saved_metrics DB round trip (define, redefine/upsert, delete)")
    finally:
        delete_saved_metric(conn, metric_name)
        conn.close()


if __name__ == "__main__":
    test_parse_define_metric_command_basic()
    test_parse_define_metric_command_with_description()
    test_parse_define_metric_command_rejects_bad_input()
    test_render_semantic_layer_html_empty_and_populated()
    test_saved_metrics_db_round_trip()
    print("\nAll semantic_layer tests passed.")
