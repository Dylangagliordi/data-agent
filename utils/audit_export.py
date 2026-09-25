"""Full Audit Export (Spec 8, Part 5): a single table's ENTIRE recorded
history — every cleaning event and every transformation decision — across
every run this project has ever logged, not just one run's slice of it (the
existing report/presentation/lineage tools all narrate a single question's
run; this walks the full timeline for one table instead).

Reads straight from the two log files this project already keeps
(logs/cleaning_log.jsonl, logs/query_log.jsonl) — no DB, no LLM, no new
write path. Matching is case-insensitive on table_name, since
cleaning_log.jsonl's table_name is the original CSV stem (not necessarily
lowercase) while query_log.jsonl's transformation_narrative_log entries use
whatever casing was live at decision time — see
.claude/skills/data-agent-architecture/SKILL.md's note on this same
normalization already being needed by report code.

CLI: `python main.py "audit: <table_name>"`.
"""

import html
import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLEANING_LOG_PATH = PROJECT_ROOT / "logs" / "cleaning_log.jsonl"
QUERY_LOG_PATH = PROJECT_ROOT / "logs" / "query_log.jsonl"
OUTPUT_DIR = PROJECT_ROOT / "audit_exports"


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def get_table_audit_history(table_name: str) -> dict:
    """Every cleaning event and transformation decision ever logged for
    table_name, both lists chronological (oldest first) — the log files
    themselves are append-only in time order, so no separate sort is needed.
    Returns empty lists (never an error) when nothing has ever been logged
    for this table."""
    target = table_name.lower()

    cleaning_events = []
    for entry in _read_jsonl(CLEANING_LOG_PATH):
        for file_rec in entry.get("files", []):
            if file_rec.get("table_name", "").lower() != target:
                continue
            cleaning_events.append(
                {
                    "timestamp": entry.get("timestamp"),
                    "trigger": entry.get("trigger"),
                    "file_name": file_rec.get("file_name"),
                    "status": file_rec.get("status"),
                    "row_count_before": file_rec.get("row_count_before"),
                    "row_count_after": file_rec.get("row_count_after"),
                    "issues_found": file_rec.get("issues_found", []),
                    "issues_resolved": file_rec.get("issues_resolved", []),
                    "issues_still_unresolved": file_rec.get("issues_still_unresolved", []),
                }
            )

    transformation_decisions = []
    for entry in _read_jsonl(QUERY_LOG_PATH):
        for item in entry.get("transformation_narrative_log", []):
            if item.get("table_name", "").lower() != target:
                continue
            transformation_decisions.append(
                {
                    "timestamp": entry.get("timestamp"),
                    "user_question": entry.get("user_question"),
                    "candidate_kind": (item.get("candidate") or {}).get("kind"),
                    "chosen_option_id": item.get("chosen_option_id"),
                    "fresh": item.get("fresh"),
                    "reload_reask": item.get("reload_reask"),
                    "decided_at": item.get("decided_at"),
                }
            )

    return {
        "table_name": table_name,
        "cleaning_events": cleaning_events,
        "transformation_decisions": transformation_decisions,
    }


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def _cleaning_events_html(events: list) -> str:
    if not events:
        return "<p><em>No cleaning events recorded for this table.</em></p>"
    rows = "".join(
        f"<tr><td>{_esc(e['timestamp'])}</td><td>{_esc(e['file_name'])}</td><td>{_esc(e['trigger'])}</td>"
        f"<td>{_esc(e['status'])}</td><td>{_esc(e['row_count_before'])} &rarr; {_esc(e['row_count_after'])}</td>"
        f"<td>{len(e['issues_resolved'])} resolved, {len(e['issues_still_unresolved'])} unresolved</td></tr>"
        for e in events
    )
    return (
        "<table><thead><tr><th>When</th><th>File</th><th>Trigger</th><th>Status</th>"
        f"<th>Row count</th><th>Issues</th></tr></thead><tbody>{rows}</tbody></table>"
    )


def _transformation_decisions_html(decisions: list) -> str:
    if not decisions:
        return "<p><em>No transformation decisions recorded for this table.</em></p>"
    rows = "".join(
        f"<tr><td>{_esc(d['timestamp'])}</td><td>{_esc(d['user_question'])}</td>"
        f"<td>{_esc(d['candidate_kind'])}</td><td>{_esc(d['chosen_option_id'])}</td>"
        f"<td>{'fresh decision' if d['fresh'] else 'reused cached decision'}"
        f"{' (reload re-ask)' if d.get('reload_reask') else ''}</td></tr>"
        for d in decisions
    )
    return (
        "<table><thead><tr><th>When</th><th>Question that surfaced it</th>"
        "<th>Candidate</th><th>Chosen option</th><th>Freshness</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
    )


def render_table_audit_html(table_name: str) -> str:
    history = get_table_audit_history(table_name)
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    full_html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Audit History</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 960px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
table {{ border-collapse: collapse; width: 100%; margin: 0.5rem 0 1.5rem; font-size: 0.92em; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; vertical-align: top; }}
th {{ background: #f2f5f9; }}
.meta {{ color: #666; font-size: 0.88em; }}
</style>
</head>
<body>
<h1>Audit History: {_esc(table_name)}</h1>
<p class="meta">Generated {generated_at} &mdash; {len(history['cleaning_events'])} cleaning event(s), \
{len(history['transformation_decisions'])} transformation decision(s)</p>
<h2>Cleaning events</h2>
{_cleaning_events_html(history['cleaning_events'])}
<h2>Transformation decisions</h2>
{_transformation_decisions_html(history['transformation_decisions'])}
</body>
</html>
"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = "".join(c if c.isalnum() else "_" for c in table_name.lower())
    out_path = OUTPUT_DIR / f"{safe_name}.html"
    out_path.write_text(full_html, encoding="utf-8")
    return str(out_path)
