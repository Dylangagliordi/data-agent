"""Tests for utils/generate_presentation.py.

Test 1: present last — reads most recent log entry, produces a navigable HTML slideshow.
Test 2: present: <question> pattern covered by checking generate_presentation() directly
        with a fresh log entry.
Test 3: Real cleaning history → Issue/Solution slides present.
Test 4: No cleaning history → cleaning slides correctly omitted.
Test 5: Real visualize: entry → chart image embedded as base64.
Test 6: Navigation structure — prev/next buttons and JS are present.
"""

import json
import re
from pathlib import Path

from utils.generate_presentation import generate_presentation
from utils.generate_report import last_query_log_entry

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
assert 'class="slide"' in html1, "Must have slide elements"
assert "What are we exploring?" in html1, "Slide 2 must be present"

print(f"PASS: navigable slideshow at {pres_path1}\n")


# ── Test 2: generate_presentation with a sql_analyst entry ────────────────────
print("=" * 70)
print("TEST 2: sql_analyst entry → slideshow has Title, Exploring, Question, Summary")
print("=" * 70)

entries = _load_log_entries()
sql_entries = [e for e in entries if e.get("route_response") == "sql_analyst"]
assert sql_entries, "Need at least one sql_analyst entry"

entry2 = sql_entries[-1]
print(f"Using entry: {entry2.get('user_question')!r}")
pres_path2 = generate_presentation(entry2)
html2 = Path(pres_path2).read_text()

question_text = entry2.get("user_question") or entry2.get("curated_question") or ""
assert "What are we exploring?" in html2, "Slide 2 header must be present"
assert "Key Takeaways" in html2, "Summary slide must be present"

# Title slide contains the question
assert question_text[:40] in html2 or any(
    part in html2 for part in question_text.split()[:4]
), "Title slide must contain the question"

print(f"PASS: sql_analyst slideshow has all required sections.\n")


# ── Test 3: real cleaning history → Issue/Solution slides present ─────────────
print("=" * 70)
print("TEST 3: real cleaning history → Issue/Solution slides present")
print("=" * 70)

# Find a query log entry that touches a table with cleaning history.
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
    pres_path3 = generate_presentation(entry3)
    html3 = Path(pres_path3).read_text()

    assert "Uncleaned Data" in html3, "Cleaning history → Uncleaned Data slide must be present"
    assert "Issue" in html3 and "Solution" in html3, (
        "Cleaning history → Issue/Solution slide labels must be present"
    )
    assert "Cleaned Data" in html3, "Cleaned Data slide must be present"
    # The issue-label and solution-label divs are the key markers
    assert 'class="issue-label"' in html3, "issue-label div must be present"
    assert 'class="solution-label"' in html3, "solution-label div must be present"
    print(f"PASS: Issue/Solution slides present in {pres_path3}\n")


# ── Test 4: no cleaning history → cleaning slides correctly omitted ───────────
print("=" * 70)
print("TEST 4: no cleaning history → cleaning slides correctly omitted")
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
    "sql_query_execution_result": "[{'total': 32951}]",
    "final_answer": "There are 32,951 products.",
}

# olist_products_dataset has no cleaning history; if it does, test is still valid
# because we check that issue-label only appears when there IS history.
pres_path4 = generate_presentation(synthetic_no_clean)
html4 = Path(pres_path4).read_text()

# Verify the mandatory slides are still present.
assert "What are we exploring?" in html4, "Slide 2 must still be present"
assert "Key Takeaways" in html4, "Summary slide must still be present"

# Only assert cleaning slides absent if the table genuinely has no history.
products_cleaned = any(
    f.get("table_name", "").lower() == "olist_products_dataset"
    for e in cleaning_entries for f in e.get("files", [])
    if f.get("issues_resolved")
)
if not products_cleaned:
    assert 'class="issue-label"' not in html4, (
        "No cleaning history → Issue/Solution slides must be omitted"
    )
    assert "Uncleaned Data" not in html4, (
        "No cleaning history → Uncleaned Data slide must be omitted"
    )
    print("PASS: cleaning slides correctly omitted when no history exists.\n")
else:
    print("NOTE: olist_products_dataset has cleaning history — omission assertion skipped.\n")


# ── Test 5: visualize entry → chart image embedded as base64 ──────────────────
print("=" * 70)
print("TEST 5: visualize entry with chart_image_path → base64 image in slideshow")
print("=" * 70)

from agents.sql_analyst import build_visualization
from models.schema import SQLAnalystState

FAKE_RESULT = str([
    {"customer_state": "SP", "order_count": 41746},
    {"customer_state": "RJ", "order_count": 12852},
    {"customer_state": "MG", "order_count": 11635},
])

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
}

pres_path5 = generate_presentation(synthetic_viz)
html5 = Path(pres_path5).read_text()

assert "Visualization" in html5, "Visualization slide must be present for visualize: entries"
assert "data:image/png;base64," in html5, (
    "Chart image must be embedded as base64 when chart_image_path is set"
)
assert '<img class="chart"' in html5, "chart img tag must be present"
print(f"PASS: chart image embedded as base64 in {pres_path5}\n")

# Test 5b: reasoned chart type shows reasoning on the slide.
synthetic_reasoned = dict(synthetic_viz)
synthetic_reasoned["chart_type_source"] = "reasoned"
synthetic_reasoned["chart_type_reasoning"] = "A bar chart best compares discrete state counts."
pres_path5b = generate_presentation(synthetic_reasoned)
html5b = Path(pres_path5b).read_text()
assert "Why this chart type" in html5b, (
    "Reasoned chart type must show reasoning on the visualization slide"
)
assert "bar chart best compares" in html5b, "Actual reasoning text must appear"
print("PASS: reasoned chart type shows reasoning.\n")

# Test 5c: no chart_image_path → honest 'no image' note, no broken img tag.
synthetic_no_img = dict(synthetic_viz)
synthetic_no_img["chart_image_path"] = ""
pres_path5c = generate_presentation(synthetic_no_img)
html5c = Path(pres_path5c).read_text()
assert "data:image/png;base64," not in html5c, (
    "No base64 when chart_image_path is absent"
)
print("PASS: no chart_image_path → no broken image.\n")


# ── Test 6: navigation — correct slide count in JS show() call ─────────────────
print("=" * 70)
print("TEST 6: navigation structure — JS initialises from slide 0")
print("=" * 70)

assert "show(0)" in html1, "JS must call show(0) on load to initialise first slide"
slide_count = html1.count('class="slide"')
print(f"  Slide count in test-1 file: {slide_count}")
assert slide_count >= 3, f"Must have at least 3 slides (title, exploring, summary), got {slide_count}"
print("PASS: navigation structure correct.\n")


print("=" * 70)
print("ALL GENERATE_PRESENTATION TESTS PASSED")
print("=" * 70)
