"""
One-off script: build the scatter-plot PNG answering the "top-rated vs
best-paying industries" question, from the CSV the SQL analyst's
build_visualization node just produced.

Columns in the CSV: Industry, Num Jobs, Avg Rating, Pct Top Rated Employers, Avg Salary
X axis: Pct Top Rated Employers (concentration of 4.0+ rated employers)
Y axis: Avg Salary
Point size: Num Jobs (sample size backing each industry's numbers)
Quadrant lines at the median of each axis highlight the "well-paying
alternative in a low-satisfaction industry" opportunity zone (low
concentration of top-rated employers, high pay).
"""
import csv
import statistics
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

CSV_PATH = Path("outputs/visualizations/which_industries_have_the_highest_concen_20260902_180801.csv")
OUT_PATH = Path("outputs/visualizations/industry_rating_vs_pay_scatter_20260902.png")

rows = []
with open(CSV_PATH, newline="") as f:
    reader = csv.DictReader(f)
    for r in reader:
        rows.append({
            "industry": r["Industry"],
            "num_jobs": int(r["Num Jobs"]),
            "avg_rating": float(r["Avg Rating"]),
            "pct_top_rated": float(r["Pct Top Rated Employers"]),
            "avg_salary": float(r["Avg Salary"]),
        })

pct_values = [r["pct_top_rated"] for r in rows]
salary_values = [r["avg_salary"] for r in rows]
median_pct = statistics.median(pct_values)
median_salary = statistics.median(salary_values)

fig, ax = plt.subplots(figsize=(11, 7.5))

sizes = [r["num_jobs"] * 6 + 40 for r in rows]
colors = ["#c0392b" if r["pct_top_rated"] < median_pct and r["avg_salary"] > median_salary
          else "#7f8c8d" for r in rows]

ax.scatter(pct_values, salary_values, s=sizes, c=colors, alpha=0.75, edgecolors="black", linewidths=0.6, zorder=3)

for r in rows:
    ax.annotate(
        r["industry"],
        (r["pct_top_rated"], r["avg_salary"]),
        textcoords="offset points",
        xytext=(6, 4),
        fontsize=8,
        zorder=4,
    )

ax.axvline(median_pct, color="grey", linestyle="--", linewidth=1, zorder=1)
ax.axhline(median_salary, color="grey", linestyle="--", linewidth=1, zorder=1)

ax.text(
    median_pct - (max(pct_values) - min(pct_values)) * 0.02,
    max(salary_values) - (max(salary_values) - min(salary_values)) * 0.02,
    "OPPORTUNITY ZONE:\nhigh pay, low satisfaction\n(\"well-paying alternative\")",
    ha="right", va="top", fontsize=9, color="#c0392b", fontweight="bold",
    bbox=dict(boxstyle="round", fc="#fdecea", ec="#c0392b", alpha=0.9),
)

ax.set_xlabel("% of employers rated 4.0+ (concentration of top-rated employers)", fontsize=11)
ax.set_ylabel("Average salary estimate ($)", fontsize=11)
ax.set_title(
    "Industry satisfaction vs. pay: where a well-paying alternative could win talent\n"
    "(point size = number of job postings backing that industry's numbers)",
    fontsize=12,
)
ax.yaxis.set_major_formatter(lambda x, pos: f"${x:,.0f}")
ax.grid(True, alpha=0.25, zorder=0)

fig.tight_layout()
fig.savefig(OUT_PATH, dpi=150)
print(f"Saved chart to {OUT_PATH.resolve()}")
print(f"median pct_top_rated={median_pct}, median avg_salary={median_salary}")
