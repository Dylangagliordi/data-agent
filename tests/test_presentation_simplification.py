"""Tests for the presentation "sound more human" changes to
utils/generate_presentation.py:

Test 1: _resolve_category_values — deterministic category-column detection
        against a real-shaped result, no LLM.
Test 2: _generate_glossary — one batched LLM call, parses `label :: explanation`
        lines back onto the real category values; malformed response -> {}.
Test 3: _resolve_scatter_columns — deterministic shape check (needs a
        category column, a count-like column, and 2+ other numeric measures);
        None when the shape doesn't fit.
Test 4: _render_scatter_chart_b64 — renders real base64 PNG bytes from a
        resolved scatter spec.
Test 5: _simplify_cleaning_narratives — one batched call rewrites N solution
        texts into N plain-language ones, order-preserved; malformed/short
        response -> None (caller must fall back to raw text).
Test 6: end-to-end generate_presentation() on the real last query_log entry
        still produces a valid slideshow (regression check for the wiring).
"""

import base64
import json
from pathlib import Path

from utils.generate_presentation import (
    _generate_glossary,
    _render_scatter_chart_b64,
    _resolve_category_values,
    _resolve_scatter_columns,
    _simplify_cleaning_narratives,
    generate_presentation,
)
from utils.generate_report import last_query_log_entry

FAKE_RESULT = json.dumps({
    "columns": ["industry", "avg_salary_k", "avg_rating", "job_count"],
    "rows": [
        ["Staffing & Outsourcing", 128.97, 4.14, 35],
        ["Federal Agencies", 134.59, 3.96, 11],
        ["Consulting", 128.10, 3.89, 29],
    ],
    "truncated": False,
})


class FakeGlossaryLLM:
    def invoke(self, messages):
        class R:
            content = (
                "Staffing & Outsourcing :: Companies that supply workers to other businesses.\n"
                "Federal Agencies :: U.S. government departments and organizations.\n"
                "Consulting :: Firms that advise other businesses.\n"
            )
        return R()


class MalformedGlossaryLLM:
    def invoke(self, messages):
        class R:
            content = "I'm not sure what these mean."
        return R()


class FailingLLM:
    def invoke(self, messages):
        raise RuntimeError("simulated LLM failure")


class FakeSimplifyLLM:
    def invoke(self, messages):
        # Echo back N plain rewrites, --- separated, matching whatever count
        # was asked for (parsed from the prompt's own "exactly N" phrasing).
        prompt = messages[0][1]
        import re
        m = re.search(r"exactly (\d+) rewritten", prompt)
        n = int(m.group(1)) if m else 1
        rewrites = [f"Plain rewrite #{i+1}." for i in range(n)]
        class R:
            content = "\n---\n".join(rewrites)
        return R()


class ShortSimplifyLLM:
    """Returns fewer parts than requested — must be rejected (None), never
    silently mismatched onto the wrong issues."""
    def invoke(self, messages):
        class R:
            content = "Only one rewrite."
        return R()


# ── Test 1: _resolve_category_values — deterministic, no LLM ──────────────────
print("=" * 70)
print("TEST 1: _resolve_category_values picks the real non-numeric column")
print("=" * 70)

entry1 = {"sql_query_execution_result": FAKE_RESULT}
resolved1 = _resolve_category_values(entry1)
assert resolved1 is not None, "Must resolve a category column from a real result"
cat_col, values = resolved1
assert cat_col == "industry", f"Expected 'industry', got {cat_col!r}"
assert values == ["Staffing & Outsourcing", "Federal Agencies", "Consulting"], (
    f"Must return real distinct values in first-seen order, got {values!r}"
)
print(f"PASS: resolved category column {cat_col!r} with values {values!r}\n")

# All-numeric result -> None (nothing to build a glossary of)
entry1b = {"sql_query_execution_result": json.dumps({
    "columns": ["a", "b"], "rows": [[1, 2], [3, 4]], "truncated": False,
})}
assert _resolve_category_values(entry1b) is None, "All-numeric result must resolve to None"
print("PASS: all-numeric result correctly resolves to None.\n")


# ── Test 2: _generate_glossary — parses label::explanation, honest on failure ──
print("=" * 70)
print("TEST 2: _generate_glossary parses real lines; fails honestly")
print("=" * 70)

glossary = _generate_glossary("industry", values, FakeGlossaryLLM())
assert set(glossary.keys()) == set(values), f"Every real value must get an entry: {glossary!r}"
assert "supply workers" in glossary["Staffing & Outsourcing"], "Real explanation must be used"
print(f"PASS: glossary resolved for all {len(values)} real terms.\n")

glossary_bad = _generate_glossary("industry", values, MalformedGlossaryLLM())
assert glossary_bad == {}, "Malformed response must yield an empty (honest) glossary, not fabricate one"
print("PASS: malformed LLM response yields empty glossary, not fabrication.\n")

glossary_fail = _generate_glossary("industry", values, FailingLLM())
assert glossary_fail == {}, "LLM exception must be caught and yield empty glossary"
print("PASS: LLM exception handled without raising.\n")


# ── Test 3: _resolve_scatter_columns — deterministic shape check ──────────────
print("=" * 70)
print("TEST 3: _resolve_scatter_columns requires category + count + 2 measures")
print("=" * 70)

entry3 = {"sql_query_execution_result": FAKE_RESULT}
resolved3 = _resolve_scatter_columns(entry3)
assert resolved3 is not None, "Real 4-column shape (category, 2 measures, count) must resolve"
assert resolved3["category_col"] == "industry"
assert resolved3["count_col"] == "job_count", f"Must pick the real count-like column, got {resolved3['count_col']!r}"
assert set([resolved3["x_col"], resolved3["y_col"]]) == {"avg_salary_k", "avg_rating"}, (
    f"The two non-count numeric columns must be the x/y measures, got {resolved3!r}"
)
print(f"PASS: resolved scatter spec {resolved3['category_col']}/{resolved3['x_col']}/{resolved3['y_col']}/{resolved3['count_col']}\n")

# No count-like column -> None
entry3b = {"sql_query_execution_result": json.dumps({
    "columns": ["industry", "avg_salary_k", "avg_rating"],
    "rows": [["A", 1.0, 2.0], ["B", 3.0, 4.0]],
    "truncated": False,
})}
assert _resolve_scatter_columns(entry3b) is None, "No count-like column must resolve to None"
print("PASS: missing count-like column correctly resolves to None.\n")

# Only 1 measure column -> None
entry3c = {"sql_query_execution_result": json.dumps({
    "columns": ["industry", "avg_salary_k", "job_count"],
    "rows": [["A", 1.0, 5], ["B", 3.0, 9]],
    "truncated": False,
})}
assert _resolve_scatter_columns(entry3c) is None, "Only 1 measure column must resolve to None"
print("PASS: single-measure result correctly resolves to None.\n")


# ── Test 4: _render_scatter_chart_b64 — real PNG bytes ─────────────────────────
print("=" * 70)
print("TEST 4: _render_scatter_chart_b64 renders real base64 PNG bytes")
print("=" * 70)

b64 = _render_scatter_chart_b64(resolved3)
assert b64, "Must render real base64 bytes for a valid scatter spec"
png_bytes = base64.b64decode(b64)
assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n", "Must be a real PNG file signature"
print(f"PASS: rendered {len(png_bytes)} real PNG bytes.\n")


# ── Test 5: _simplify_cleaning_narratives — batched, order-preserved, honest ───
print("=" * 70)
print("TEST 5: _simplify_cleaning_narratives batches N->N, fails honestly")
print("=" * 70)

items = [
    ("Placeholder values: column 'Rating' has -1 sentinels", "# Replace -1 with NaN"),
    ("Currency/unit symbols: column 'Salary' has $/K", "# Strip $ and K suffixes"),
]
simplified = _simplify_cleaning_narratives(items, FakeSimplifyLLM())
assert simplified is not None and len(simplified) == 2, f"Must return exactly 2 rewrites, got {simplified!r}"
assert simplified[0] == "Plain rewrite #1." and simplified[1] == "Plain rewrite #2.", (
    f"Order must be preserved: {simplified!r}"
)
print(f"PASS: batched simplification returned {len(simplified)} order-preserved rewrites.\n")

mismatched = _simplify_cleaning_narratives(items, ShortSimplifyLLM())
assert mismatched is None, "A response with the wrong count must be rejected (None), never mismatched onto issues"
print("PASS: count-mismatched response correctly rejected as None.\n")

empty_items = _simplify_cleaning_narratives([], FakeSimplifyLLM())
assert empty_items == [], "No items -> empty list, no LLM call needed"
print("PASS: no items short-circuits to an empty list.\n")


# ── Test 6: end-to-end regression — generate_presentation still works ─────────
print("=" * 70)
print("TEST 6: generate_presentation(last_entry) still produces a valid slideshow")
print("=" * 70)

entry6 = last_query_log_entry()
assert entry6 is not None, "Need at least one entry in query_log.jsonl"
pres_path = generate_presentation(entry6)
pres = Path(pres_path)
assert pres.exists() and pres.stat().st_size > 0
html_text = pres.read_text()
assert 'id="prev"' in html_text and 'id="next"' in html_text
print(f"PASS: regenerated a valid slideshow at {pres_path}\n")


print("=" * 70)
print("ALL PRESENTATION SIMPLIFICATION TESTS PASSED")
print("=" * 70)
