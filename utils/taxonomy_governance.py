"""Taxonomy Governance (Spec 8, Part 2): browse and diff the versioned
reference-mapping files Manual Mode already writes
(utils/manual_mode.py:save_reference_mapping_file, under
utils/reference_mappings/<base_name>_v<N>.json).

This is a pure READ over data that already exists — Manual Mode is still the
only thing that ever writes a reference mapping file ("never overwriting a
prior version"); this module just makes that history browsable as a first-
class library instead of something only discoverable by listing a directory,
and shows what actually changed between consecutive versions of the same
column's mapping.

CLI: `python main.py "taxonomy"`.
"""

import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
REFERENCE_MAPPINGS_DIR = PROJECT_ROOT / "utils" / "reference_mappings"
OUTPUT_DIR = PROJECT_ROOT / "taxonomy_governance"

_VERSION_RE = re.compile(r"^(.+)_v(\d+)\.json$")


def list_reference_mappings() -> list:
    """Every saved reference-mapping file, grouped by base_name (e.g.
    "industry_categories"), each with its full version history oldest-first.
    Returns [] if the directory doesn't exist or holds nothing yet — a fresh
    project that has never run Manual Mode, not an error.

    Each group: {"base_name": str, "versions": [payload, ...]} where each
    payload is exactly what save_reference_mapping_file wrote (column,
    version, declared_category_count, reference_source, provenance,
    created_at, mapping).
    """
    if not REFERENCE_MAPPINGS_DIR.exists():
        return []

    by_base: dict[str, list] = {}
    for path in REFERENCE_MAPPINGS_DIR.glob("*_v*.json"):
        m = _VERSION_RE.match(path.name)
        if not m:
            continue
        base_name = m.group(1)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        by_base.setdefault(base_name, []).append(payload)

    groups = []
    for base_name, payloads in by_base.items():
        payloads.sort(key=lambda p: p.get("version", 0))
        groups.append({"base_name": base_name, "versions": payloads})
    groups.sort(key=lambda g: g["base_name"])
    return groups


def diff_mapping_versions(older: dict, newer: dict) -> dict:
    """Compares two versions' real "mapping" dicts (raw_value -> group).
    Returns {"added": [...raw values new to `newer`], "removed": [...raw
    values present in `older` but gone from `newer`], "changed": [(raw_value,
    old_group, new_group), ...]} — every list sorted for stable output.
    """
    older_map = older.get("mapping", {}) if older else {}
    newer_map = newer.get("mapping", {}) if newer else {}

    added = sorted(set(newer_map) - set(older_map))
    removed = sorted(set(older_map) - set(newer_map))
    changed = sorted(
        (k, older_map[k], newer_map[k])
        for k in (set(older_map) & set(newer_map))
        if older_map[k] != newer_map[k]
    )
    return {"added": added, "removed": removed, "changed": changed}


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _diff_html(diff: dict) -> str:
    if not diff["added"] and not diff["removed"] and not diff["changed"]:
        return "<p><em>No changes from the previous version.</em></p>"
    parts = []
    if diff["added"]:
        parts.append("<p><b>Added:</b> " + ", ".join(_esc(v) for v in diff["added"]) + "</p>")
    if diff["removed"]:
        parts.append("<p><b>Removed:</b> " + ", ".join(_esc(v) for v in diff["removed"]) + "</p>")
    if diff["changed"]:
        rows = "".join(
            f"<tr><td>{_esc(v)}</td><td>{_esc(old)}</td><td>{_esc(new)}</td></tr>"
            for v, old, new in diff["changed"]
        )
        parts.append(
            "<p><b>Regrouped:</b></p>"
            "<table class='diff'><thead><tr><th>Raw value</th><th>Old group</th>"
            f"<th>New group</th></tr></thead><tbody>{rows}</tbody></table>"
        )
    return "\n".join(parts)


def _mapping_table_html(mapping: dict) -> str:
    if not mapping:
        return "<p><em>Empty mapping.</em></p>"
    rows = "".join(
        f"<tr><td>{_esc(raw)}</td><td>{_esc(group)}</td></tr>"
        for raw, group in sorted(mapping.items())
    )
    return f"<table><thead><tr><th>Raw value</th><th>Group</th></tr></thead><tbody>{rows}</tbody></table>"


def render_taxonomy_governance_html() -> str:
    """Renders every tracked column's full reference-mapping version history
    into one page: the latest version's full mapping, plus a diff against the
    version before it (when one exists). Returns the written path — always
    succeeds, even with zero mappings on record (renders an honest empty
    page rather than erroring)."""
    groups = list_reference_mappings()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if not groups:
        sections_html = "<p><em>No reference mappings have been saved via Manual Mode yet.</em></p>"
    else:
        section_parts = []
        for group in groups:
            versions = group["versions"]
            latest = versions[-1]
            previous = versions[-2] if len(versions) >= 2 else None
            diff_html = _diff_html(diff_mapping_versions(previous, latest)) if previous else (
                "<p><em>This is the first recorded version — nothing to diff against.</em></p>"
            )
            version_badges = ", ".join(f"v{p['version']}" for p in versions)
            section_parts.append(
                f"<h2>{_esc(latest.get('column', group['base_name']))}</h2>"
                f"<p class='meta'>Versions on record: {version_badges} &mdash; "
                f"current: v{latest['version']}, saved {_esc(latest.get('created_at'))}, "
                f"source: {_esc(latest.get('reference_source'))}</p>"
                f"<h3>Change since v{previous['version'] if previous else '-'}</h3>"
                f"{diff_html}"
                f"<h3>Current mapping (v{latest['version']})</h3>"
                f"{_mapping_table_html(latest.get('mapping', {}))}"
            )
        sections_html = "\n".join(section_parts)

    full_html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Taxonomy Governance</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
h2 {{ border-bottom: 2px solid #ddd; padding-bottom: 6px; margin-top: 2.5rem; }}
table {{ border-collapse: collapse; width: 100%; margin: 0.5rem 0 1rem; font-size: 0.92em; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; }}
th {{ background: #f2f5f9; }}
table.diff td:nth-child(2) {{ color: #a33; text-decoration: line-through; }}
table.diff td:nth-child(3) {{ color: #2a7a2a; font-weight: 600; }}
.meta {{ color: #666; font-size: 0.88em; }}
</style>
</head>
<body>
<h1>Taxonomy Governance</h1>
<p class="meta">Generated {generated_at} &mdash; {len(groups)} tracked column(s)</p>
{sections_html}
</body>
</html>
"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "taxonomy.html"
    out_path.write_text(full_html, encoding="utf-8")
    return str(out_path)
