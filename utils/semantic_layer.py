"""Semantic Layer (Spec 8, Part 1): a small, explicit registry of human-named,
reusable metric definitions, backed by the `_saved_metrics` Postgres table
(utils/load_data.py:ensure_saved_metrics_table/write_saved_metric/
read_saved_metrics/delete_saved_metric).

This is deliberately NOT an automatic system: a metric is only ever added,
changed, or removed by an explicit `define metric:` / `delete metric:` CLI
call — never inferred from a query someone happened to ask. Once defined, its
name and SQL fragment are surfaced as extra, non-authoritative context inside
agents/sql_analyst.py:add_context, the same additive way fan-out/DQ warnings
already are — generate_sql is told a canonical definition exists, but nothing
mechanically forces it to use one instead of writing its own SQL.

CLI:
    python main.py "define metric: <name> = <sql_fragment>"
    python main.py "define metric: <name> = <sql_fragment> -- <description>"
    python main.py "delete metric: <name>"
    python main.py "metrics"
"""

import html
import re
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "semantic_layer"

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def parse_define_metric_command(raw: str) -> dict:
    """Parses the text after "define metric: " into {"metric_name",
    "sql_fragment", "description"}. Raises ValueError with a clear message on
    anything malformed — never silently guesses at intent.

    Format: "<name> = <sql_fragment>" with an optional "-- <description>"
    trailer (SQL's own comment syntax, so it reads naturally next to a real
    SQL fragment). The first "=" splits name from fragment; only the LAST
    "--" splits fragment from description, since a fragment itself is never
    expected to contain a literal "--" but could plausibly contain "=" only
    once, at the top level, for this simple case.
    """
    if "=" not in raw:
        raise ValueError(
            "expected 'define metric: <name> = <sql_fragment>' — no '=' found"
        )
    name_part, rest = raw.split("=", 1)
    metric_name = name_part.strip()
    if not _NAME_RE.match(metric_name):
        raise ValueError(
            f"metric name {metric_name!r} must be a valid identifier "
            "(letters, digits, underscores, not starting with a digit)"
        )

    description = ""
    if "--" in rest:
        rest, description = rest.rsplit("--", 1)
        description = description.strip()

    sql_fragment = rest.strip()
    if not sql_fragment:
        raise ValueError(f"metric {metric_name!r} has an empty SQL fragment")

    return {"metric_name": metric_name, "sql_fragment": sql_fragment, "description": description}


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def render_semantic_layer_html(metrics: list) -> str:
    """Render every defined metric as one browsable page. Writes into
    semantic_layer/ and returns the path. metrics is read_saved_metrics()'s
    real return value — this function does no DB access itself, so it stays
    trivially testable against a fake list."""
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not metrics:
        rows_html = "<tr><td colspan='4'><em>No metrics defined yet.</em></td></tr>"
    else:
        rows_html = "".join(
            "<tr>"
            f"<td><code>{_esc(m['metric_name'])}</code></td>"
            f"<td><pre>{_esc(m['sql_fragment'])}</pre></td>"
            f"<td>{_esc(m['description'])}</td>"
            f"<td>{_esc(m['updated_at'])}</td>"
            "</tr>"
            for m in metrics
        )

    full_html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Semantic Layer</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; vertical-align: top; }}
th {{ background: #f2f5f9; }}
pre {{ margin: 0; white-space: pre-wrap; font-size: 0.9em; }}
.meta {{ color: #666; font-size: 0.88em; }}
</style>
</head>
<body>
<h1>Semantic Layer</h1>
<p class="meta">Generated {generated_at} &mdash; {len(metrics)} metric(s) defined</p>
<table>
<thead><tr><th>Name</th><th>SQL fragment</th><th>Description</th><th>Last updated</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
</body>
</html>
"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "metrics.html"
    out_path.write_text(full_html, encoding="utf-8")
    return str(out_path)
