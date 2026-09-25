# Analyst Judgment Rubric

Every SQL analyst answer applies the rules below. Rules 1–8 are enforced mechanically
through prompt constraints and deterministic post-execution disclosure. Rules 9–11,
13, and 14 are enforced through the prompt only (no post-execution extraction
possible). Every triggered rule is disclosed in the "How this answer was computed"
block that appears at the bottom of the answer when one or more rules fire.

---

## Rules

### Rule 1 — Minimum sample size for per-category rankings
When a question asks to rank, compare, or identify the best/top/highest category by
an averaged or rate-based metric, the query must apply `HAVING COUNT(*) >= 5` unless
the question explicitly states a different minimum. This value is fixed project-wide
(not a fresh judgment call per query) so the same question produces the same
included/excluded categories every run. A category with 1–4 underlying rows is one
anecdote, not a reliable average.

**Disclosure trigger:** `HAVING COUNT(*) >= N` detected in executed SQL.

---

### Rule 2 — Combined-metric ranking transparency
When a question asks to optimize for more than one metric at once (e.g. "maximize
both salary and job satisfaction"), the query must use an explicit `ORDER BY` over
all stated metrics — never silently pick only one while mentioning the other in
prose. The primary metric goes first in the `ORDER BY`; secondary and tertiary
metrics follow in the order the query author judged to be most representative.

**Disclosure trigger:** `ORDER BY` with 2+ columns detected in executed SQL.

---

### Rule 3 — NULL/missing value honesty
Aggregate functions (`SUM`, `AVG`, `COUNT(col)`) already skip NULLs — that is the
correct behavior. The query must never substitute 0 for a genuinely missing value
with `COALESCE(col, 0)` unless the question explicitly asks for missing-as-zero
treatment. When the query filters NULLs out (via `WHERE col IS NOT NULL` or
`COUNT(col)` vs `COUNT(*)`), that exclusion must be literal in the SQL text so it
can be detected and disclosed automatically.

**Disclosure trigger:** `col IS NOT NULL` pattern detected in executed SQL.

---

### Rule 4 — Time-framing disclosure
When the query filters on a date or time column, the date-range boundaries must
appear as literal values in the SQL text (string literals, `BETWEEN` constants,
`date_trunc`/`EXTRACT` expressions). The range is extracted from the SQL after
execution and disclosed in the answer so users know exactly what time window the
result covers — not a summary like "recent orders" with no stated boundary.

**Disclosure trigger:** date literal, `BETWEEN`, `date_trunc`, or `EXTRACT` detected
in executed SQL.

---

### Rule 5 — Outlier sensitivity
When the result contains a numeric metric column and one group's value is
substantially higher than the median of the rest (> 3× the group median), a caution
note is appended. A single extreme value in a small-to-medium group can dominate an
average in a way that makes that group look like the clear leader when it is actually
an artefact of one data point.

**Disclosure trigger:** outlier ratio > 3× median detected in parsed result data.

---

### Rule 6 — Group size imbalance
When the result includes a count-like column and the largest group has 10× or more
rows than the smallest, a note is appended. Comparing an average computed over 2
rows to one computed over 2,000 rows is not an apples-to-apples comparison — the
uncertainty is orders of magnitude different even if the averages happen to be close.

**Disclosure trigger:** max/min count ratio ≥ 10 detected in parsed result data.

---

### Rule 7 — Unit normalization
When comparing a total or summed metric across groups of different sizes (e.g. total
revenue by region, where regions have different numbers of orders), prefer a per-unit
metric (average revenue per order) unless the question explicitly asks for totals.
Never mix total for one group with an average for another in the same result.

**Enforcement:** SQL-generation prompt rule (no post-execution extraction).

---

### Rule 8 — Association vs causation
SQL reads data — it never establishes why something is true, only what it is. The
final answer must never use causal language ("causes", "leads to", "responsible for",
"explains why", "because of") when referring to an observed association. If the
LLM-written answer contains such language, a correction note is appended automatically.

**Disclosure trigger:** causal-phrase regex detected in the LLM-written answer text.

---

### Rule 9 — Fan-out / grain safety
Before writing an aggregation that spans more than one table, the query must check
whether any joined table could have more than one row per the unit being measured.
If so, that table must be aggregated down to one row per key in a subquery or CTE
before being joined. The database context injected before generation includes explicit
fan-out warnings for tables confirmed to have this issue.

**Enforcement:** SQL-generation prompt rule + live fan-out detection in `add_context`.

---

### Rule 10 — Result completeness vs. convenience
A `LIMIT` clause must not be added when the question genuinely calls for every
matching row (e.g. "list all customers", "export all orders"). Conversely, an
unbounded query must not be written when the question implies a small, specific
answer (e.g. "the top 5", "which category", "how many"). Match the `LIMIT`
(or absence of one) to what the question actually asks for.

**Enforcement:** SQL-generation prompt rule (no post-execution extraction).

---

### Rule 11 — Read-only scope
The analyst only reads data — it never performs write operations. If the question
asks for an action (insert, update, delete, tier assignment, "send", etc.), the
query must answer the read-only part of the question plainly (e.g. "which customers
spent the most") and not fake the action by adding a literal constant column that
pretends it happened.

**Enforcement:** SQL-generation prompt rule + `is_safe` judge gate blocks any
non-read-only query from execution.

---

### Rule 12 — Duplicate-row transparency
When the query removes duplicate rows via `SELECT DISTINCT`, this is disclosed in the
answer. `SELECT DISTINCT` silently assumes repeated identical rows are data-entry
artefacts (true duplicates), not legitimate repeated observations (e.g. a customer
placing two identical orders). The assumption is often correct but is never obvious
from the result alone.

**Disclosure trigger:** `SELECT DISTINCT` detected in executed SQL.

---

### Rule 13 — Composite-value splitting
When a column in the schema encodes two or more logically distinct values in one
field (e.g. a `city, state` combined string, a `80000-100000` salary range, a job
title with seniority appended), and the question asks about one component, the query
must extract that component using `SPLIT_PART`, `SUBSTRING`, `REGEXP_REPLACE`, or
a `CASE WHEN` expression — not GROUP BY the combined field as an opaque string.

**Enforcement:** SQL-generation prompt rule (no post-execution extraction; the
composite structure must be visible in the schema sample rows).

---

### Note on Rule 8 — Causal language rewrite (not just disclaimer)
`_apply_causal_correction` uses a two-pass approach:
1. Unambiguously causal phrases (`leads to`, `causes`, `caused by`, `results in`,
   `responsible for`, `driven by`, `explains why`, `because of`, `due to`) are
   **replaced in-place** with associative equivalents (`is associated with`,
   `alongside`, etc.).
2. Ambiguous verbs (`drives`, `affects`, `impacts`) that survive pass 1 trigger
   the association disclaimer as a fallback — replacing these risks breaking
   legitimate non-causal uses.

This is a rewrite, not just a disclaimer bolted on: a reader who stops reading
early should not still see the false causal claim.

---

### Rule 14 — NULL-filter placement for per-category rankings
For any query that ranks/compares categories by an averaged or rate-based metric,
every `col IS NOT NULL` filter feeding that metric must be applied in ONE `WHERE`
clause in the query's base CTE/subquery, before any `GROUP BY` — never split across
stages, and never applied only after an earlier stage has already aggregated.
Splitting or relocating these filters changes which underlying rows count toward
each group's average, silently producing a different row count and average for the
exact same question across separate runs — the same class of bug Rule 1's fixed
`HAVING COUNT(*) >= 5` threshold exists to prevent, applied to filter placement
instead of the sample-size threshold.

**Enforcement:** SQL-generation prompt rule (no post-execution extraction — filter
placement relative to `GROUP BY` isn't mechanically distinguishable from the SQL
text alone the way Rule 3's `IS NOT NULL` presence is). Regression-tested live in
`tests/test_null_filter_placement_consistency.py`, which runs the exact "Of the 5
highest-paying industries..., which offer the highest employee satisfaction?"
question 3 times and asserts identical per-industry job_count/avg_salary across runs.

---

### Rule 15 — Statistical significance for grouped comparisons

Comparing bare averages across groups (Rules 5/6's outlier/imbalance heuristics) never
answers whether an observed difference is real or could plausibly be due to normal
variation. When a query ranks/compares groups by an averaged metric, it should also
compute that metric's `STDDEV(...)` and `COUNT(...)` alongside its `AVG(...)` — the three
real sufficient statistics a proper significance test needs per group — so a genuine
one-way ANOVA can be run instead of eyeballing the averages alone.

**Disclosure trigger:** the executed SQL's result already contains a matched
`avg_<x>`/`mean_<x>`, `stddev_<x>`/`std_<x>`, and a count column for the same metric
across 2+ groups (`_significance_test_note`). Never estimated or fabricated when any of
the three ingredients is missing from the result — this is purely reactive to what the
query actually returned, with a prompt-only reminder (like Rule 1's original form)
encouraging `generate_sql` to include them, never a mechanical retry.

**Enforcement:** SQL-generation prompt rule (`_rubric_applicable_instructions`'s existing
ranking/comparison reminder) + post-execution disclosure via `_significance_test_note`,
wired into `_analyst_judgment_disclosure` alongside Rules 5/6. The ANOVA F-statistic and
p-value are computed analytically from each group's real (mean, stddev, n) — mathematically
identical to running the test on the raw underlying rows, since ANOVA never needs more than
those three numbers per group — with the p-value read from `scipy.stats.f`'s survival
function. Unit-tested against hand-built result rows (clearly significant, clearly not,
and every "ingredients missing" case) plus a live query against the real olist database in
`tests/test_significance_testing.py`.

---

## Out of scope

The following are explicitly not handled by the SQL analyst and no rubric rule
applies to them:

- **Data cleaning / imputation at query time** — normalizing casing, trimming
  whitespace, filling nulls, replacing placeholder values, deduplicating rows:
  that is `clean_dataset()`'s job, which runs at load time. The analyst assumes
  it is querying already-clean data.
- **Persistent, multi-table data models** — building a relationship layer
  equivalent to Power BI's DAX model or Tableau's calculated fields. Each
  invocation produces one flat, correctly-shaped result for one specific question.
- **Multi-step / transactional operations** — any operation that requires more
  than one SQL statement to complete atomically.
- **Forecasting or predictive modeling** — all results describe what the data
  contains, not what it might contain in the future.
