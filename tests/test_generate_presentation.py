"""Tests for utils/generate_presentation.py (Spec 2: slideshow body = the
same shared narrative walkthrough utils/generate_report.py renders as HTML,
utils/narrative.py).

Content-exactness checks target build_narrative_walkthrough's DETERMINISTIC
step objects directly (pre-narration — the LLM narration pass legitimately
paraphrases prose, so exact substrings aren't stable to assert post-narration).
generate_presentation()-level checks are structural: the right part dividers /
step titles / real embedded media are present.

Test 1: present last — reads most recent log entry, produces a navigable HTML slideshow.
Test 2: sql_analyst entry → title slide + Part A/Part C dividers present.
Test 3: real cleaning history → real issue/batch step slides present under Part A.
Test 4: no cleaning history → Part A still present (per Spec 2: every table gets a
        step), but states honestly that there's no history — no issue-fix slides.
Test 5: visualize entry → chart image embedded as base64; reasoned chart type's
        real reasoning appears in the deterministic step; no chart_image_path →
        honest 'no image' note, no broken <img> tag.
Test 6: Navigation structure — prev/next buttons and JS are present.
"""

import json
import re
from pathlib import Path

from utils.generate_presentation import generate_presentation
from utils.generate_report import last_query_log_entry
from utils.narrative import build_narrative_walkthrough

LOG_PATH = Path("logs/query_log.jsonl")
CLEANING_LOG_PATH = Path("logs/cleaning_log.jsonl")


def _load_log_entries() -> list:
    if not LOG_PATH.exists():
        return []
    return [json.loads(l) for l in LOG_PATH.read_text().splitlines() if l.strip()]


# ── Test 1: present last — produces a real, navigable slideshow ───────────────
print("=" * 70)
print("TEST 1: present last → generate_presentation(last_entry) → navigable HTML")
print("=" * 70)

entry1 = last_query_log_entry()
assert entry1 is not None, "Need at least one entry in query_log.jsonl"

pres_path1 = generate_presentation(entry1)
print(f"Presentation written to: {pres_path1}")

pres1 = Path(pres_path1)
assert pres1.exists(), f"Presentation file must exist: {pres_path1}"
assert pres1.stat().st_size > 0, "Presentation file must not be empty"
assert pres1.suffix == ".html", "Presentation must be an HTML file"
assert "presentations" in pres_path1, "Must be written to the presentations/ directory"

html1 = pres1.read_text()

# Navigation structure
assert 'id="prev"' in html1, "Must have a prev button"
assert 'id="next"' in html1, "Must have a next button"
assert 'id="counter"' in html1, "Must have a slide counter element"
assert "<script>" in html1, "Must contain JS"
assert "show(" in html1, "JS must contain the show() navigation function"

# Required slides always present
assert 'class="slide' in html1, "Must have slide elements"
assert "Part A: Data Cleaning" in html1, "Part A divider slide must be present"

print(f"PASS: navigable slideshow at {pres_path1}\n")


# ── Test 2: sql_analyst entry → title + part dividers ──────────────────────────
print("=" * 70)
print("TEST 2: sql_analyst entry → title slide + Part A/Part C dividers")
print("=" * 70)

entries = _load_log_entries()
sql_entries = [e for e in entries if e.get("route_response") == "sql_analyst"]
assert sql_entries, "Need at least one sql_analyst entry"

entry2 = sql_entries[-1]
print(f"Using entry: {entry2.get('user_question')!r}")
pres_path2 = generate_presentation(entry2)
html2 = Path(pres_path2).read_text()

assert "Part A: Data Cleaning" in html2, "Part A divider must be present"
assert "Part C: Analysis" in html2, "Part C divider must be present"
assert "Part B: Transformation Options" not in html2, (
    "a real logged entry without transformation_narrative_log must omit Part B entirely"
)

question_text = entry2.get("user_question") or entry2.get("curated_question") or ""
assert question_text[:40] in html2 or any(
    part in html2 for part in question_text.split()[:4]
), "Title slide must contain the question"

print("PASS: sql_analyst slideshow has all required parts.\n")


# ── Test 3: real cleaning history → real issue/batch step slides present ──────
print("=" * 70)
print("TEST 3: real cleaning history → real issue/batch step slides present")
print("=" * 70)

cleaning_entries = []
if CLEANING_LOG_PATH.exists():
    for line in CLEANING_LOG_PATH.read_text().splitlines():
        try:
            cleaning_entries.append(json.loads(line))
        except json.JSONDecodeError:
            pass

cleaned_tables = {
    f.get("table_name", "").lower()
    for e in cleaning_entries
    for f in e.get("files", [])
    if f.get("issues_resolved")
}

entry3 = None
for e in entries:
    sql = e.get("generated_sql_query", "")
    for t in cleaned_tables:
        if t and re.search(r"\b" + re.escape(t) + r"\b", sql, re.IGNORECASE):
            entry3 = e
            break
    if entry3:
        break

if entry3 is None:
    print("SKIP: no log entry touches a table with resolved cleaning history.\n")
else:
    print(f"Using entry: {entry3.get('user_question')!r}")
    steps3 = build_narrative_walkthrough(entry3)
    cleaning_steps3 = [s for s in steps3 if s.part == "cleaning"]
    assert any(s.stats.get("resolved") for s in cleaning_steps3), (
        "expected at least one resolved cleaning step for this table"
    )

    pres_path3 = generate_presentation(entry3)
    html3 = Path(pres_path3).read_text()
    assert "Part A: Data Cleaning" in html3
    # At least one real step title from the deterministic walkthrough must
    # appear verbatim as a slide heading (titles are never narrated/rewritten).
    resolved_titles = [s.title for s in cleaning_steps3 if s.stats.get("resolved")]
    assert any(t in html3 for t in resolved_titles), (
        f"expected one of {resolved_titles} to appear as a slide title in {pres_path3}"
    )
    print(f"PASS: real issue/batch step slides present in {pres_path3}\n")


# ── Test 4: no cleaning history → honest statement, no fabricated fix slides ──
print("=" * 70)
print("TEST 4: no cleaning history → Part A still present, states honestly")
print("=" * 70)

synthetic_no_clean = {
    "timestamp": "2026-01-01T00:00:00+00:00",
    "route_response": "sql_analyst",
    "route_comments": "",
    "user_question": "How many products are in the database?",
    "curated_question": "How many products are in the database?",
    "generated_sql_query": "SELECT COUNT(*) AS total FROM olist_products_dataset;",
    "is_safe": "yes",
    "comments": "",
    "sql_query_execution_result": json.dumps({"columns": ["total"], "rows": [[32951]], "truncated": False}),
    "final_answer": "There are 32,951 products.",
}

pres_path4 = generate_presentation(synthetic_no_clean)
html4 = Path(pres_path4).read_text()

assert "Part A: Data Cleaning" in html4, "Part A divider must still be present (Spec 2: every table gets a step)"
assert "Part C: Analysis" in html4, "Part C divider must still be present"

products_cleaned = any(
    f.get("table_name", "").lower() == "olist_products_dataset"
    for e in cleaning_entries for f in e.get("files", [])
    if f.get("issues_resolved")
)
if not products_cleaned:
    assert "No cleaning history" in html4, (
        "must state honestly that this table has no cleaning history"
    )
    assert "Fix the same" not in html4 and "Split " not in html4, (
        "no fabricated fix/split slides may appear when there is no real cleaning history"
    )
    print("PASS: no cleaning history stated honestly, no fabricated fix slides.\n")
else:
    print("NOTE: olist_products_dataset has cleaning history — honesty assertion skipped.\n")


# ── Test 5: visualize entry → chart image embedded as base64 ──────────────────
print("=" * 70)
print("TEST 5: visualize entry with chart_image_path → base64 image in slideshow")
print("=" * 70)

from agents.sql_analyst import build_visualization
from models.schema import SQLAnalystState

import json as _json_pres
FAKE_RESULT = _json_pres.dumps({
    "columns": ["customer_state", "order_count"],
    "rows": [["SP", 41746], ["RJ", 12852], ["MG", 11635]],
    "truncated": False,
})

state5 = SQLAnalystState(
    wants_visualization=True,
    user_question="Show a bar chart of orders per state",
    curated_question="Show a bar chart of orders per state.",
    chart_type="bar chart",
    chart_type_source="explicit",
    chart_type_reasoning="",
    sql_query_execution_result=FAKE_RESULT,
)
viz_result = build_visualization(state5)

synthetic_viz = {
    "timestamp": "2026-09-03T00:00:00+00:00",
    "route_response": "visualize",
    "route_comments": "",
    "user_question": "Show a bar chart of orders per state",
    "curated_question": "Show a bar chart of orders per state.",
    "chart_type": "bar chart",
    "chart_type_source": "explicit",
    "chart_type_reasoning": "",
    "generated_sql_query": "SELECT customer_state, COUNT(*) AS order_count FROM orders GROUP BY customer_state",
    "is_safe": "yes",
    "sql_query_execution_result": FAKE_RESULT,
    "output_file_path": viz_result["output_file_path"],
    "chart_image_path": viz_result["chart_image_path"],
    "final_answer": "Bar chart saved.",
    "transformation_narrative_log": [],
    "transformation_candidates_not_relevant": [],
}

pres_path5 = generate_presentation(synthetic_viz)
html5 = Path(pres_path5).read_text()

assert "Visualize the result as a bar chart" in html5, "the chart step must be present for visualize: entries"
assert "data:image/png;base64," in html5, (
    "Chart image must be embedded as base64 when chart_image_path is set"
)
assert '<img class="chart"' in html5, "chart img tag must be present"
print(f"PASS: chart image embedded as base64 in {pres_path5}\n")

# Test 5b: reasoned chart type includes the real reasoning (checked at the
# deterministic step level — pre-narration).
synthetic_reasoned = dict(synthetic_viz)
synthetic_reasoned["chart_type_source"] = "reasoned"
synthetic_reasoned["chart_type_reasoning"] = "A bar chart best compares discrete state counts."
steps5b = build_narrative_walkthrough(synthetic_reasoned)
chart_step5b = next(s for s in steps5b if s.title.startswith("Visualize the result"))
assert "A bar chart best compares discrete state counts." in chart_step5b.explanation
pres_path5b = generate_presentation(synthetic_reasoned)
assert Path(pres_path5b).exists()
print("PASS: reasoned chart type's real reasoning appears in the deterministic step.\n")

# Test 5c: no chart_image_path → honest 'no image' note, no broken img tag.
synthetic_no_img = dict(synthetic_viz)
synthetic_no_img["chart_image_path"] = ""
pres_path5c = generate_presentation(synthetic_no_img)
html5c = Path(pres_path5c).read_text()
assert "data:image/png;base64," not in html5c, (
    "No base64 when chart_image_path is absent"
)
assert "No chart image available" in html5c
print("PASS: no chart_image_path → no broken image, honest note shown.\n")


# ── Test 6: navigation — correct slide count in JS show() call ─────────────────
print("=" * 70)
print("TEST 6: navigation structure — JS initialises from slide 0")
print("=" * 70)

assert "show(0)" in html1, "JS must call show(0) on load to initialise first slide"
slide_count = html1.count('class="slide')
print(f"  Slide count in test-1 file: {slide_count}")
assert slide_count >= 3, f"Must have at least 3 slides (title, part divider, a step), got {slide_count}"
print("PASS: navigation structure correct.\n")


print("=" * 70)
print("ALL GENERATE_PRESENTATION TESTS PASSED")
print("=" * 70)
