"""
Regression test for the NULL-rendered-as-zero bug in chart rendering
(architecture review P0): every chart-drawing helper used to compute
`_to_float(val) or 0`, so a genuinely missing (NULL) value was silently
plotted as a real zero — indistinguishable from an actual zero data point.

This directly exercises each chart-drawing helper (bar, line, pie, stacked
bar, treemap) with a dataset containing one row whose metric is a real Python
None, and confirms the NULL is either omitted or rendered with a distinct
visual marker (hatch pattern / "No data" annotation / NaN-induced line gap) —
never silently drawn identically to a real zero. Unit-level (no LLM, no DB):
these helpers take plain dict rows and a real matplotlib Axes/Figure.
"""

import math

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from agents.sql_analyst import (
    _chart_bar,
    _chart_line,
    _chart_pie,
    _chart_stacked_bar,
    _chart_treemap,
)


def _new_ax():
    fig = Figure(figsize=(6, 4))
    FigureCanvasAgg(fig)
    return fig, fig.add_subplot(111)


print("=" * 70)
print("BAR: a NULL value must be visually distinct from a real zero")
print("=" * 70)

data = [
    {"category": "A", "value": 0},      # real zero
    {"category": "B", "value": None},   # genuinely missing
    {"category": "C", "value": 5},
]
fig, ax = _new_ax()
_chart_bar(ax, data, ["category", "value"])

bars = [p for p in ax.patches]
assert len(bars) == 3, f"expected 3 bar patches, got {len(bars)}"
real_zero_bar, null_bar, real_bar = bars

assert null_bar.get_hatch() == "//", (
    f"NULL-value bar must carry a hatch marker, got hatch={null_bar.get_hatch()!r}"
)
assert real_zero_bar.get_hatch() in (None, ""), (
    f"real-zero bar must NOT carry the null-marker hatch, got hatch={real_zero_bar.get_hatch()!r}"
)
assert real_bar.get_hatch() in (None, ""), "real (non-null) bar must have no hatch marker"

annotation_texts = [t.get_text() for t in ax.texts]
assert "No data" in annotation_texts, (
    f"expected a 'No data' annotation for the NULL bar, got texts={annotation_texts}"
)
print("PASS: NULL bar is hatched + annotated 'No data'; real zero bar is a plain bar.\n")


print("=" * 70)
print("LINE: a NULL value must produce a real gap (NaN), not a plotted zero")
print("=" * 70)

data_line = [
    {"month": "Jan", "revenue": 100},
    {"month": "Feb", "revenue": None},
    {"month": "Mar", "revenue": 0},
    {"month": "Apr", "revenue": 50},
]
fig2, ax2 = _new_ax()
_chart_line(ax2, data_line, ["month", "revenue"])

line = ax2.lines[0]
y_data = list(line.get_ydata())
assert math.isnan(y_data[1]), f"NULL value at index 1 must be NaN in the plotted line, got {y_data[1]!r}"
assert y_data[2] == 0, f"real zero at index 2 must remain a real 0, got {y_data[2]!r}"
print(f"PASS: line y-data = {y_data} — NULL is NaN (a real gap), real zero stays 0.\n")


print("=" * 70)
print("PIE: a NULL-valued row must be omitted, not plotted as a same-looking zero wedge")
print("=" * 70)

data_pie = [
    {"segment": "A", "share": 40},
    {"segment": "B", "share": None},
    {"segment": "C", "share": 60},
]
fig3, ax3 = _new_ax()
_chart_pie(ax3, data_pie, ["segment", "share"])

wedges = [p for p in ax3.patches]
assert len(wedges) == 2, f"expected 2 wedges (NULL row omitted), got {len(wedges)}"
print(f"PASS: {len(wedges)} wedges drawn — the NULL-share row was omitted entirely.\n")


print("=" * 70)
print("STACKED BAR: a NULL cell must be hatched, distinct from a real-zero cell")
print("=" * 70)

data_stacked = [
    {"category": "X", "sub": "s1", "value": 0},     # real zero cell
    {"category": "X", "sub": "s2", "value": None},  # genuinely missing cell
    {"category": "Y", "sub": "s1", "value": 10},
    {"category": "Y", "sub": "s2", "value": 20},
]
fig4, ax4 = _new_ax()
_chart_stacked_bar(ax4, data_stacked, ["category", "sub", "value"])

# Two bar() calls (one per sub-category) produced 2 patches each (one per category).
hatched = [p for p in ax4.patches if p.get_hatch() == "//"]
assert len(hatched) == 1, f"expected exactly 1 hatched (NULL) segment, got {len(hatched)}"
print(f"PASS: exactly one segment hatched for the NULL cell; real-zero cell unmarked.\n")


print("=" * 70)
print("TREEMAP: a NULL-sized row must be omitted, not drawn as a real zero-area rect")
print("=" * 70)

data_tree = [
    {"category": "P", "size": 30},
    {"category": "Q", "size": None},
    {"category": "R", "size": 70},
]
fig5 = Figure(figsize=(6, 4))
FigureCanvasAgg(fig5)
_chart_treemap(fig5, data_tree, ["category", "size"])

tree_ax = fig5.axes[0]
tree_patches = [p for p in tree_ax.patches]
assert len(tree_patches) == 2, f"expected 2 treemap rectangles (NULL row omitted), got {len(tree_patches)}"
print(f"PASS: {len(tree_patches)} rectangles drawn — the NULL-size row was omitted entirely.\n")

print("=" * 70)
print("ALL CHART NULL-HANDLING ASSERTIONS PASSED")
print("=" * 70)
