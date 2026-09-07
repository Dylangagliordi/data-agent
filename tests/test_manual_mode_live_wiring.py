"""
Spec 3 acceptance tests (live graph wiring): manual mode actually wired into
agents/sql_analyst.py:surface_transformations against the real
uncleaned_ds_jobs table.

Same discipline as test_transformation_options_live_wiring.py: calls
add_context then surface_transformations directly (no LLM cost for the SQL
path — generate_sql/is_safe/execute_sql are never invoked), sys.stdin.isatty
monkeypatched True, real answers piped to actual shell stdin. Run as:
    printf 'skip\\n%.0s' {1..20} | PYTHONPATH=. uv run python tests/test_manual_mode_live_wiring.py

Test 1 (acceptance test 1): apply_manual_mode with a COMPLETE override for
the categorical_consolidation(Industry) candidate skips
present_transformation_options entirely (no "TRANSFORMATION OPTION:" banner
for it) and logs the decision to _transformation_decisions with
reasoning_shown.source == "manual_mode".

Test 2 (acceptance test 2): a candidate with no override present
(range_decomposition for size) still goes through the normal live menu
flow, unaffected by manual mode being active for the OTHER candidate
(Industry) in the same run.

Test 3 (acceptance test 3): the applied categorical_consolidation actually
wrote the real column using supplied_data exactly — spot-checked against
>= 15 of the real 57 distinct Industry values, per the spec's own acceptance
criterion.

Seeds/restores _transformation_candidates/_transformation_decisions and the
real table's CSV/DB state around the test so it leaves no residue.
"""

import re
import shutil
import sys
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from agents.sql_analyst import add_context, surface_transformations
from models.schema import SQLAnalystState
from utils.data_cleaning import _read_csv_robust
from utils.load_data import (
    get_admin_connection,
    invalidate_cached_decisions_for_table,
    read_transformation_candidates,
    read_transformation_decision,
    write_transformation_candidates,
)
from utils.manual_mode import ManualModeOverride, apply_manual_mode, clear_manual_mode
from utils.transformation_options import detect_transformation_candidates


def _offered_titles(captured_stdout: str) -> set:
    return set(re.findall(r"TRANSFORMATION OPTION: (.+)", captured_stdout))


TABLE = "uncleaned_ds_jobs"
RAW_PATH = Path("data/data-science-jobs/Uncleaned_DS_jobs.csv")
CLEANED_PATH = Path("data/data-science-jobs/cleaned/Uncleaned_DS_jobs.csv")

conn = get_admin_connection()
cleaned_backup = None
try:
    invalidate_cached_decisions_for_table(conn, TABLE)
    original_candidates = read_transformation_candidates(conn, TABLE)  # restored in finally
    if CLEANED_PATH.exists():
        cleaned_backup = CLEANED_PATH.read_bytes()

    # Detect against the REAL CLEANED file (not the raw one) — this dev
    # environment's cleaned/ artifact for this table already has sanitized,
    # lowercase column names (job_title, not "Job Title") from earlier
    # composite-field-discovery sessions (see test_transformation_options_
    # live_wiring.py's own note on this), and _apply_chosen_transformation
    # actually mutates the CLEANED file, so the candidate's column name must
    # match what's really there for TEST 3's real application to work.
    assert CLEANED_PATH.exists(), f"expected a cleaned artifact at {CLEANED_PATH}"
    seed_df = _read_csv_robust(str(CLEANED_PATH))
    candidates = detect_transformation_candidates(seed_df, TABLE)
    write_transformation_candidates(conn, TABLE, candidates)

    industry_cc = next(
        c for c in candidates if c.kind == "categorical_consolidation" and c.columns == ["industry"]
    )
    real_industries = seed_df["industry"].dropna().unique().tolist()
    assert len(real_industries) == 57, f"expected the real 57 distinct Industry values, got {len(real_industries)}"

    # Complete, deterministic supplied_data covering every real distinct
    # value (a real, if arbitrary, mapping — split by name length parity so
    # the exact assignment is trivially checkable, not a judgment call the
    # test would need to trust).
    supplied_data = {v: ("Group A" if len(v) % 2 == 0 else "Group B") for v in real_industries}

    print("=" * 70)
    print("TEST 1: apply_manual_mode with a complete override skips the live")
    print("menu entirely and logs reasoning_shown.source == 'manual_mode'")
    print("=" * 70)

    clear_manual_mode()
    apply_manual_mode([
        ManualModeOverride(
            candidate_id=industry_cc.candidate_id,
            chosen_option_id="apply",
            reference_source="Test-supplied deterministic industry name-length split",
            supplied_data=supplied_data,
        )
    ])

    state1 = SQLAnalystState(
        user_question="Consolidate industry categories and show average company size",
        curated_question="Consolidate industry categories and show average company size.",
    )
    ctx1 = add_context(state1)
    state1_with_ctx = state1.model_copy(update=ctx1)

    captured1 = StringIO()
    with patch("sys.stdin.isatty", return_value=True):
        original_stdout = sys.stdout
        sys.stdout = captured1
        try:
            result1 = surface_transformations(state1_with_ctx)
        finally:
            sys.stdout = original_stdout
    printed1 = captured1.getvalue()
    offered1 = _offered_titles(printed1)
    print("offered (live menu banners):", offered1)

    assert not any("Categorical Consolidation for industry" in t for t in offered1), (
        f"manual mode must skip the live present_transformation_options banner entirely, got {offered1}"
    )

    manual_entries = [
        e for e in result1["transformation_narrative_log"]
        if e["candidate"]["candidate_id"] == industry_cc.candidate_id
    ]
    assert len(manual_entries) == 1, manual_entries
    assert manual_entries[0]["reasoning_shown"]["source"] == "manual_mode"
    assert manual_entries[0]["reasoning_shown"]["reference"] == "Test-supplied deterministic industry name-length split"
    assert manual_entries[0]["chosen_option_id"] == "apply"

    decision_row = read_transformation_decision(conn, TABLE, industry_cc.candidate_id)
    assert decision_row is not None, "the manual-mode decision must be persisted to _transformation_decisions"
    assert decision_row["reasoning_shown"]["source"] == "manual_mode"
    print("PASS: manual mode skipped present_transformation_options entirely and logged the decision "
          "with reasoning_shown.source == 'manual_mode'.\n")

    print("=" * 70)
    print("TEST 2: a candidate with NO override present still goes through the")
    print("normal live menu, unaffected by manual mode being active elsewhere")
    print("=" * 70)

    size_rd = next(c for c in candidates if c.kind == "range_decomposition" and c.columns == ["size"])
    assert not any(
        e["candidate"]["candidate_id"] == size_rd.candidate_id for e in manual_entries
    )
    size_entries = [
        e for e in result1["transformation_narrative_log"]
        if e["candidate"]["candidate_id"] == size_rd.candidate_id
    ]
    assert len(size_entries) == 1, size_entries
    assert size_entries[0]["reasoning_shown"].get("source") != "manual_mode", (
        "a candidate with no registered override must never be routed through manual mode"
    )
    assert any("Range Decomposition for size" in t for t in offered1), (
        f"the size candidate (no override registered) must still show its live menu banner, got {offered1}"
    )
    print("PASS: the size candidate (no manual-mode override registered) went through the ordinary "
          "live present_transformation_options menu, unaffected by manual mode being active for Industry.\n")

    print("=" * 70)
    print("TEST 3: the applied categorical_consolidation used supplied_data")
    print("exactly — spot-check >= 15 of the real 57 Industry mappings")
    print("=" * 70)

    written_df = _read_csv_robust(str(CLEANED_PATH))
    assert "industry_category" in written_df.columns, (
        f"expected a new 'industry_category' column, got {list(written_df.columns)}"
    )

    checked = 0
    for _, row in written_df.head(400).iterrows():
        raw = row.get("industry")
        if raw not in supplied_data:
            continue
        expected = supplied_data[raw]
        actual = row.get("industry_category")
        assert actual == expected, f"row with industry={raw!r}: expected {expected!r}, got {actual!r}"
        checked += 1
        if checked >= 15:
            break
    assert checked >= 15, f"only spot-checked {checked} rows, need >= 15"
    print(f"PASS: spot-checked {checked} real rows — every applied category matches supplied_data exactly.\n")

    print("=" * 70)
    print("ALL MANUAL-MODE LIVE-WIRING (SPEC 3) ASSERTIONS PASSED")
    print("=" * 70)

finally:
    clear_manual_mode()
    invalidate_cached_decisions_for_table(conn, TABLE)
    write_transformation_candidates(conn, TABLE, original_candidates)
    if cleaned_backup is not None:
        CLEANED_PATH.write_bytes(cleaned_backup)
        # _apply_chosen_transformation's categorical_consolidation branch
        # reloads the live table (load_csv_to_table) with the new
        # industry_category column — restore the file first, then reload
        # the DB table back to its original schema too, so this test leaves
        # no residue in either the file or the live table.
        from utils.load_data import load_csv_to_table
        load_csv_to_table(conn, CLEANED_PATH)
    elif CLEANED_PATH.exists():
        CLEANED_PATH.unlink()
    conn.close()
