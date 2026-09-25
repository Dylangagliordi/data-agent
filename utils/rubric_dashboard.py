"""Rubric Rules Dashboard (Spec 8, Part 3): how often each analyst-judgment
rule (analyst-judgment-rubric.md) has actually fired, across every past run
in logs/query_log.jsonl.

Detection is a plain substring match against the real, already-computed text
each rule mechanically writes into `final_answer` when it fires —
agents/sql_analyst.py:_analyst_judgment_disclosure (Rules 1-6, 12, 15) and
_apply_causal_correction's _ASSOCIATION_DISCLAIMER (Rule 8) both write fixed,
non-LLM-varied phrasing every single time, so matching that phrasing verbatim
is exact, not a heuristic guess. (Rule 15's exact F/p numbers vary per run, so
its detector matches the fixed surrounding phrasing, not the numbers.)

9 of the rubric's 15 rules are covered here — the other 6 (Rules 7, 9, 10,
11, 13, 14) are explicitly documented in analyst-judgment-rubric.md as
"SQL-generation prompt rule (no post-execution extraction)": they shape what
SQL gets generated but leave no independent, mechanically-detectable trace in
a logged entry, so a historical firing count for them would have to be
invented. This module says so explicitly rather than pretending otherwise.

CLI: `python main.py "rubric dashboard"`.
"""

import html
import json
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "logs" / "query_log.jsonl"
OUTPUT_DIR = PROJECT_ROOT / "rubric_dashboard"


def _detect_min_sample(final_answer: str) -> bool:
    return "underlying rows were included in this ranking" in final_answer


def _detect_combined_ranking(final_answer: str) -> bool:
    return "This result is ranked primarily by" in final_answer


def _detect_null_exclusion(final_answer: str) -> bool:
    return "is missing (NULL) were excluded from this result" in final_answer


def _detect_time_framing(final_answer: str) -> bool:
    return any(
        phrase in final_answer
        for phrase in (
            "This result covers the period from",
            "This result is filtered to the period from",
            "This result is filtered to data around",
            "This result is grouped by",
        )
    )


def _detect_outlier_sensitivity(final_answer: str) -> bool:
    return "substantially higher than the typical group" in final_answer


def _detect_group_size_imbalance(final_answer: str) -> bool:
    return "comparing averages across groups this unevenly sized may underweight" in final_answer


def _detect_causal_correction(final_answer: str) -> bool:
    return "this result shows a statistical association only" in final_answer


def _detect_deduplication(final_answer: str) -> bool:
    return "Duplicate rows were removed from this result (SELECT DISTINCT was used)" in final_answer


def _detect_significance_test(final_answer: str) -> bool:
    return "A one-way ANOVA on" in final_answer


# Ordered exactly as numbered in analyst-judgment-rubric.md.
RULE_CATALOG = [
    {"number": 1, "name": "Minimum sample size for per-category rankings", "detector": _detect_min_sample},
    {"number": 2, "name": "Combined-metric ranking transparency", "detector": _detect_combined_ranking},
    {"number": 3, "name": "NULL/missing value honesty", "detector": _detect_null_exclusion},
    {"number": 4, "name": "Time-framing disclosure", "detector": _detect_time_framing},
    {"number": 5, "name": "Outlier sensitivity", "detector": _detect_outlier_sensitivity},
    {"number": 6, "name": "Group size imbalance", "detector": _detect_group_size_imbalance},
    {"number": 8, "name": "Association vs causation", "detector": _detect_causal_correction},
    {"number": 12, "name": "Duplicate-row transparency", "detector": _detect_deduplication},
    {"number": 15, "name": "Statistical significance for grouped comparisons", "detector": _detect_significance_test},
]

# From analyst-judgment-rubric.md's own "Enforcement: SQL-generation prompt
# rule (no post-execution extraction)" lines — real, verified, not guessed.
NOT_OBSERVABLE_RULES = [
    {"number": 7, "name": "Unit normalization"},
    {"number": 9, "name": "Fan-out / grain safety"},
    {"number": 10, "name": "Result completeness vs. convenience"},
    {"number": 11, "name": "Read-only scope"},
    {"number": 13, "name": "Composite-value splitting"},
    {"number": 14, "name": "NULL-filter placement for per-category rankings"},
]


def _read_eligible_entries() -> list:
    """Every query_log.jsonl entry that could have a rubric disclosure at all
    — sql_analyst and visualize entries (both carry final_answer built by
    represent_final_answer/build_visualization); etl_analyst entries never
    touch the SQL rubric and are excluded from the eligible-run count so the
    denominator means something."""
    if not LOG_PATH.exists():
        return []
    entries = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("route_response") in ("sql_analyst", "visualize"):
                entries.append(entry)
    return entries


def get_rubric_dashboard() -> dict:
    """Returns {"total_eligible_runs": int, "rules": [{"number", "name",
    "fire_count", "fire_rate"}, ...]} — fire_rate is fire_count/total_eligible_runs
    (0.0 when there are no eligible runs yet, never a division error)."""
    entries = _read_eligible_entries()
    total = len(entries)
    rules = []
    for rule in RULE_CATALOG:
        fire_count = sum(
            1 for e in entries if rule["detector"](e.get("final_answer", "") or "")
        )
        rules.append(
            {
                "number": rule["number"],
                "name": rule["name"],
                "fire_count": fire_count,
                "fire_rate": (fire_count / total) if total else 0.0,
            }
        )
    return {"total_eligible_runs": total, "rules": rules}


def _esc(s) -> str:
    return html.escape(str(s) if s is not None else "")


def render_rubric_dashboard_html() -> str:
    dashboard = get_rubric_dashboard()
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if dashboard["total_eligible_runs"] == 0:
        rows_html = "<tr><td colspan='4'><em>No SQL analyst runs logged yet.</em></td></tr>"
    else:
        rows_html = "".join(
            f"<tr><td>Rule {r['number']}</td><td>{_esc(r['name'])}</td>"
            f"<td>{r['fire_count']} / {dashboard['total_eligible_runs']}</td>"
            f"<td>{r['fire_rate']:.0%}</td></tr>"
            for r in dashboard["rules"]
        )

    not_observable_rows = "".join(
        f"<tr><td>Rule {r['number']}</td><td>{_esc(r['name'])}</td></tr>"
        for r in NOT_OBSERVABLE_RULES
    )

    full_html = f"""<!doctype html>
<html>
<head><meta charset="utf-8"><title>Rubric Rules Dashboard</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; color: #222; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0; }}
th, td {{ border: 1px solid #ddd; padding: 8px 12px; text-align: left; }}
th {{ background: #f2f5f9; }}
.meta {{ color: #666; font-size: 0.88em; }}
.note {{ background: #fff8e1; border-left: 4px solid #f9a825; padding: 10px 14px; border-radius: 3px; margin: 1.5rem 0 0.5rem; font-size: 0.92em; }}
</style>
</head>
<body>
<h1>Rubric Rules Dashboard</h1>
<p class="meta">Generated {generated_at} &mdash; {dashboard['total_eligible_runs']} eligible run(s) (SQL analyst + visualize)</p>
<table>
<thead><tr><th>Rule</th><th>Name</th><th>Fired</th><th>Rate</th></tr></thead>
<tbody>{rows_html}</tbody>
</table>
<div class="note">
<b>Not shown above:</b> Rules 7, 9, 10, 11, 13, 14 are enforced only as SQL-generation
prompt rules with no post-execution disclosure — analyst-judgment-rubric.md documents
each of these explicitly as leaving no independent trace in a logged run, so a
historical firing count for them would have to be invented rather than measured.
</div>
<table>
<thead><tr><th>Rule</th><th>Name (not independently observable)</th></tr></thead>
<tbody>{not_observable_rows}</tbody>
</table>
</body>
</html>
"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "rubric_dashboard.html"
    out_path.write_text(full_html, encoding="utf-8")
    return str(out_path)
