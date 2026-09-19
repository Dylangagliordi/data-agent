"""
Shared data-cleaning core, used by clean_data.py, the enhanced load_data.py, and the
ETL analyst's transform_load tool — one real implementation, not three copies.

Piece 1 (this section): a deterministic, no-LLM rubric checker. Given a single raw file,
inspect it against the rubric from the spec and return a list of concrete issue strings
(empty list == "nothing flagged, leave this file alone"). This never touches the LLM and
never modifies anything on disk — it only reads and reports.

Rubric checked here, per file, original 8 categories:
- Missing values beyond a reasonable threshold in columns that matter
- Duplicate rows, or duplicate values in a column that should be unique
- Wrong data types (numbers stored as text, inconsistent date formats)
- Inconsistent categorical values (the same real value written different ways)
- Formatting noise (stray whitespace, inconsistent capitalization)
- Invalid or clearly-impossible values (negative counts, out-of-range dates)
- Encoding problems (garbled or mixed character encoding)
- Structural issues (inconsistent column counts, malformed rows)

Plus 17 more, added later, same discipline (deterministic, no LLM, naming heuristics
rather than hardcoded per-dataset column names):

Value-level:
- Placeholder values masquerading as real data ("9999", "N/A", "TBD", "Unknown", ...)
- Inconsistent boolean representations ("Y"/"N" vs "yes"/"no" vs "1"/"0" mixed)
- Lost leading zeros in columns that look like they should preserve them (zip/postal)
- Currency or unit symbols embedded in an otherwise-numeric column ("$50.00", "50 kg")
- Locale-specific number formatting mixed within one column (1,234.56 vs 1.234,56)
- Excessive floating-point noise (19.989999999999998-style computation artifacts)
- Non-printable/control characters embedded in text fields
- Copy-paste artifacts from spreadsheet tools (literal formulas, Excel error strings)

Structural:
- Inconsistent delimiters within what looks like the same kind of multi-value field
- Column header issues (whitespace, inconsistent casing, duplicate header names)
- Column misalignment from unmatched quote characters (beyond raw column-count checks)
- A header row duplicated mid-file
- Trailing empty rows or a trailing empty (unnamed) column
- A byte-order-mark corrupting the first column name
- Special characters in column names that would break downstream tooling

Relational (within a single file only -- cross-table fan-out stays out of scope, see below):
- Inconsistent granularity within one datetime column (day-level mixed with second-level)
- Dangling references: a column that looks like a self-referencing FK whose values don't
  all exist in this file's own identifier column (only checked where a candidate primary
  identifier column is actually findable in the same file)

Deliberately NOT checked here: statistical outliers and cross-field logical consistency
(both need real judgment, not a fixed rule -- a weak approximation would be worse than
nothing), and fan-out (multiple rows per a foreign key relative to another table). Fan-out
is a cross-table relationship only checkable via information_schema once data is already
loaded into Postgres, and it's already handled live by agents/sql_analyst.py's add_context
/ _detect_fanout_warnings. Duplicating it here against raw, not-yet-loaded files would be
redundant and would need its own (different) implementation since there's no foreign-key
structure to check against pre-load.
"""

import csv
import hashlib
import json
import re
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from models.schema import ExplorationHypothesis, VerifiedPatternProposal

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CLEANING_LOG_PATH = _PROJECT_ROOT / "logs" / "cleaning_log.jsonl"

# Missing-values threshold: a column with more than this fraction of nulls/blank is
# flagged. 5% is a deliberately conservative default — real data almost always has a
# few genuinely missing values; this is meant to catch columns that are substantially
# incomplete, not to flag every dataset with a handful of nulls.
MISSING_VALUE_THRESHOLD = 0.05

# ---------------------------------------------------------------------------
# Discovery-phase config (see explore_and_verify below): an LLM-driven "glance and
# notice" pass that mimics a human scanning sorted(df[col].unique()) and going
# "huh, that's weird" — something check_rubric's closed catalog of pattern-matchers
# structurally cannot do, since it only catches shapes someone anticipated in
# advance. Everything this phase notices is mechanically re-verified against the
# real full column before it's allowed to become an actual issue (see
# _verify_hypothesis) — the LLM is never trusted on its own word.
# ---------------------------------------------------------------------------

# Per-column sample size for explore_column's "glance" — mimics how much of a
# column a human would actually scan by eye before noticing something odd.
EXPLORE_SAMPLE_SIZE = 40

# Fraction of EXPLORE_SAMPLE_SIZE dedicated to the least-frequent distinct values
# in the column (the rest is a plain random sample). Rare/low-frequency values are
# where a one-off oddity like "Healthfirst\n3.1" actually lives — pure-random
# sampling would almost never surface it in a large column.
EXPLORE_RARE_VALUE_FRACTION = 0.5

# Cap on how many column pairs _explore_column_pairs will ever send to the LLM
# for one table, to keep the cross-column pass from becoming O(n^2) in cost on a
# wide table.
EXPLORE_MAX_COLUMN_PAIRS = 10

# Hard ceiling on total LLM calls (column exploration + hypothesis verification +
# column-pair exploration, combined) explore_and_verify will make for one table.
# If hit before everything is explored, a warning is logged and whatever was
# verified so far is returned — this never raises and never silently truncates.
EXPLORE_MAX_LLM_CALLS_PER_TABLE = 60

# A column whose values average longer than this (characters) is treated as
# long-form prose (e.g. a "Job Description" column) rather than a candidate for
# composite-field discovery — skipped entirely to avoid wasted LLM calls on
# columns where "two variables glued together" isn't a meaningful question.
_EXPLORE_PROSE_MEAN_LEN_THRESHOLD = 200

# Real full-column overlap fraction at or above which two columns are considered
# "suspiciously identical" by _explore_column_pairs' mechanical verification step.
_COLUMN_PAIR_OVERLAP_THRESHOLD = 0.95

# Minimum non-null row count a column must have before discovery will even
# attempt it (explore_column) or accept a verified pattern for it
# (_verify_hypothesis). Below this, a proposed regex almost always just
# describes the tiny sample verbatim — a 5-row column where every value happens
# to be a single digit will "verify" against literally any narrow pattern that
# matches those 5 digits, which is a tautology, not evidence of a real
# composite-field structure. Applied in both places as defense in depth (the
# same layered-checks pattern already used elsewhere in this codebase, e.g.
# is_safe's deterministic AST check plus a secondary LLM sanity check).
_EXPLORE_MIN_COLUMN_ROWS = 20

# Column-name substrings that suggest a column should hold non-negative counts/quantities/
# amounts — used only for the "impossible negative value" check. This is a heuristic on
# naming convention (like generate_sql's fan-out FK heuristic), not a hardcoded list of
# real dataset column names.
NON_NEGATIVE_NAME_HINTS = ("count", "qty", "quantity", "amount", "price", "value", "total", "age")

# Column-name substrings that suggest a column is meant to be a unique identifier for a
# row in this file (e.g. "id", "_id", "code") — used only for the duplicate-unique-value
# check. Again a naming heuristic, not hardcoded per-dataset names.
UNIQUE_ID_NAME_HINTS = ("id", "code", "key")

# Tokens that commonly stand in for a genuine missing value without actually being parsed
# as NaN by pandas' default na_values list (that list already swallows things like "N/A",
# "NULL", "NaN" before we ever see them as strings) — used only for the placeholder-value
# check below, to catch the ones pandas does NOT already treat as missing.
PLACEHOLDER_TOKENS = {"9999", "999", "-1", "tbd", "unknown", "missing", "xxx", "n/a", "na", "null", "none"}

# Boolean representation "families" — a column made up entirely of tokens from more than
# one of these families (e.g. some rows "Y"/"N", others "1"/"0") is inconsistent; a column
# using tokens from exactly one family is a normal, consistent boolean column.
BOOLEAN_FAMILIES = {
    "y/n": {"y", "n"},
    "yes/no": {"yes", "no"},
    "true/false": {"true", "false"},
    "1/0": {"1", "0"},
    "t/f": {"t", "f"},
}

# Column-name substrings that suggest a column should preserve leading zeros (postal/zip
# codes are the classic case: "02139" losing its leading zero becomes "2139").
ZIP_NAME_HINTS = ("zip", "postal", "postcode")

# Currency symbols and unit suffixes checked for embedding in an otherwise-numeric column.
CURRENCY_SYMBOLS = "$€£¥"
UNIT_SUFFIXES = ("kg", "g", "lb", "lbs", "cm", "mm", "km", "mi", "oz", "%")

# Literal Excel error strings that indicate a copy-paste artifact from a spreadsheet tool.
# Deliberately excludes "#N/A" — pandas' default na_values list already converts that to a
# real NaN before this module ever sees it as a string, so it's caught by the existing
# missing-values check instead.
EXCEL_ERROR_TOKENS = {"#DIV/0!", "#REF!", "#VALUE!", "#NAME?", "#NULL!", "#NUM!"}

# ---------------------------------------------------------------------------
# Severity mapping: every issue category check_rubric() can produce is classified as
# either FAIL (a real correctness/integrity risk — silently wrong values, broken joins,
# data that downstream tooling would misread) or WARN (cosmetic/low-risk — annoying but
# doesn't corrupt meaning). This mapping did not exist before this restructuring; it was
# built fresh here by asking "does leaving this specific category unfixed produce
# SILENTLY WRONG data or broken tooling, or is it cosmetic/informational" for every one
# of the 25 categories check_rubric() checks. It is a real, editable judgment call, not a
# hidden assumption — if a category's placement here turns out wrong for how this project
# actually uses its data, move its prefix between the two sets below; nothing else in this
# module needs to change to support that.
#
# Classified as FAIL — leaving these unfixed risks silently wrong values, broken
# joins/lookups, or data downstream tooling can't parse at all:
#   - Duplicate rows / duplicate values in a column that should be unique (wrong counts,
#     broken one-to-one assumptions)
#   - Wrong data types (numbers stored as text, inconsistent date formats — arithmetic and
#     date comparisons silently misbehave or fail)
#   - Invalid/impossible values (a negative count, an out-of-range date — clearly wrong,
#     not just unusual)
#   - Encoding problems (garbled text corrupts every downstream read of that column)
#   - Structural issues (ragged rows, unparseable file — the file may not even load)
#   - Placeholder values masquerading as real data (silently wrong aggregates: a "9999"
#     stored as a real age, for example)
#   - Lost leading zeros (silently wrong identifiers — a zip code becomes a different one)
#   - Locale-specific number formatting mixed in one column (a "1.234,56" parsed as the
#     number 1.234 instead of 1234.56 — silently wrong by 1000x)
#   - Copy-paste spreadsheet artifacts (a literal formula string or error code stored in
#     place of a real, computed value — the "value" itself is not data)
#   - Column misalignment from unmatched quotes (fields silently shifted into the wrong
#     column)
#   - Header row duplicated mid-file (a literal header row masquerading as a data row)
#   - Byte-order-mark corruption (corrupts the FIRST COLUMN'S NAME — every downstream
#     reference to that column by name silently breaks)
#   - Dangling references (a foreign-key-shaped column pointing at nothing — joins
#     silently drop or misjoin rows)
#
# Classified as WARN — cosmetic, informational, or judgment-dependent; doesn't corrupt
# meaning on its own and is safe to batch:
#   - Missing values (already has its own proportional-imputation-vs-drop reasoning; not
#     a correctness bug by itself, a data-completeness question)
#   - Inconsistent categorical values (same real value, different spelling/casing — an
#     annoyance for grouping, not silently wrong data)
#   - Formatting noise (stray whitespace — cosmetic)
#   - Inconsistent boolean representations (still a real True/False either way, just
#     styled two ways)
#   - Currency/unit symbols embedded in a numeric column (the true number's still visible
#     in the string; it just needs parsing)
#   - Excessive floating-point noise (the value is still numerically correct to
#     many-decimal precision, just noisy-looking)
#   - Non-printable/control characters (usually a display/tooling annoyance, not a wrong
#     value)
#   - Inconsistent delimiters in a multi-value field (a parsing nuisance, not wrong data)
#   - Column header issues — whitespace/casing/duplicate names (annoying, not wrong values)
#   - Trailing empty rows/columns (dead weight, not wrong data)
#   - Special characters in headers (a tooling-compatibility nuisance, not wrong data)
#   - Inconsistent datetime granularity (still a valid, parseable timestamp either way)
FAIL_LEVEL_PREFIXES = (
    "Duplicate rows:",
    "Duplicate values:",
    "Wrong data type:",
    "Invalid values:",
    "Encoding problem:",
    "Structural issue:",
    "Placeholder values:",
    "Lost leading zeros:",
    "Locale-specific number formatting:",
    "Spreadsheet artifacts:",
    "Column misalignment:",
    "Header row duplicated mid-file:",
    "Byte-order-mark:",
    "Dangling references:",
    # Discovery-phase issues (see explore_and_verify below): an LLM-noticed pattern
    # that was mechanically verified against the REAL full column before ever
    # becoming an issue string — a genuinely undiscovered structural problem
    # silently corrupting downstream joins/grouping meets the same fail-level bar
    # as the statically-detected categories above, so it flows through the
    # identical approval-gate + fix pipeline with no new code path.
    "Composite field (discovered):",
    "Duplicate column (discovered):",
)

WARN_LEVEL_PREFIXES = (
    "Missing values:",
    "Inconsistent categorical values:",
    "Formatting noise:",
    "Inconsistent boolean representations:",
    "Currency/unit symbols:",
    "Excessive floating-point noise:",
    "Non-printable characters:",
    "Inconsistent delimiters:",
    "Column header issues:",
    "Trailing empty rows:",
    "Trailing empty column:",
    "Special characters in headers:",
    "Inconsistent granularity:",
    "Structural noise in prose:",
)


def _issue_severity(issue: str) -> str:
    """Classify a single check_rubric() issue string as "fail" or "warn" using
    FAIL_LEVEL_PREFIXES / WARN_LEVEL_PREFIXES above. An issue string that matches
    neither list (should never happen for a category check_rubric() actually
    produces — this is a defensive default, not an expected path) is treated as
    "warn" rather than silently dropped or crashing, so a future new category added
    to check_rubric() without updating this mapping fails safe (batched, lower
    urgency) instead of being skipped from cleaning entirely.
    """
    for prefix in FAIL_LEVEL_PREFIXES:
        if issue.startswith(prefix):
            return "fail"
    for prefix in WARN_LEVEL_PREFIXES:
        if issue.startswith(prefix):
            return "warn"
    return "warn"


def _split_issues_by_severity(issues: list) -> tuple:
    """Split a full check_rubric() issue list into (fail_issues, warn_issues),
    preserving the original order within each group."""
    fail_issues = [i for i in issues if _issue_severity(i) == "fail"]
    warn_issues = [i for i in issues if _issue_severity(i) == "warn"]
    return fail_issues, warn_issues


def _read_csv_robust(path) -> pd.DataFrame:
    """Read a CSV into a DataFrame the same way everywhere in this module that needs
    real column values (check_rubric's content checks, and the LLM-prompt context
    builder) — robust to the exact kind of encoding problem the rubric itself checks
    for. Tries UTF-8 first; on a decode failure, falls back to latin-1 (which never
    raises on arbitrary byte values), so a file that's flagged for an encoding problem
    can still be read well enough to describe its columns/sample rows to the LLM,
    instead of crashing before cleaning code can even be generated for it.
    """
    try:
        return pd.read_csv(path, dtype=str, keep_default_na=True, on_bad_lines="skip")
    except UnicodeDecodeError:
        return pd.read_csv(
            path, dtype=str, keep_default_na=True, on_bad_lines="skip", encoding="latin-1"
        )


def _read_raw_bytes(path: Path) -> bytes:
    """Read the file as raw bytes, for structural/encoding checks that need to see the
    file before any parser normalizes or silently skips malformed rows."""
    with open(path, "rb") as f:
        return f.read()


def _check_encoding(raw_bytes: bytes) -> list:
    issues = []
    try:
        raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        issues.append(
            f"Encoding problem: file is not valid UTF-8 (decode error: {e}); likely "
            "garbled or mixed character encoding."
        )
        return issues
    # Valid UTF-8 but still contains the Unicode replacement character usually means an
    # earlier lossy re-encoding already happened upstream.
    text = raw_bytes.decode("utf-8", errors="ignore")
    if "\ufffd" in text:
        issues.append(
            "Encoding problem: file contains the Unicode replacement character "
            "(U+FFFD), indicating data was previously mis-decoded/garbled."
        )
    return issues


def _check_structural(path: Path, raw_bytes: bytes) -> list:
    """Detect inconsistent column counts / malformed rows via a raw csv.reader pass
    (pandas' C parser can silently coerce or skip ragged rows depending on settings,
    so this check reads the file independently at the csv module level)."""
    issues = []
    text = raw_bytes.decode("utf-8", errors="replace")
    reader = csv.reader(text.splitlines())
    try:
        header = next(reader)
    except StopIteration:
        issues.append("Structural issue: file is empty (no header row).")
        return issues
    expected_len = len(header)
    bad_rows = 0
    total_rows = 0
    for row in reader:
        total_rows += 1
        if len(row) != expected_len:
            bad_rows += 1
    if bad_rows:
        issues.append(
            f"Structural issue: {bad_rows} of {total_rows} data rows have a different "
            f"column count than the header ({expected_len} columns) — malformed/ragged rows."
        )
    return issues


def _check_missing_values(df: pd.DataFrame) -> list:
    issues = []
    n = len(df)
    if n == 0:
        return issues
    for col in df.columns:
        null_frac = df[col].isna().mean()
        if null_frac > MISSING_VALUE_THRESHOLD:
            issues.append(
                f"Missing values: column '{col}' is {null_frac:.1%} missing/blank "
                f"(threshold {MISSING_VALUE_THRESHOLD:.0%})."
            )
    return issues


_ROW_INDEX_NAME_HINTS = ("index", "row_id", "rownum", "row_number")


def _looks_like_row_index_column(name: str, series: pd.Series) -> bool:
    """True when a column is an obvious raw row-index carried over from the
    source file (e.g. pandas' own default index re-saved as a real 'index'
    column, or an 'Unnamed: 0' column from a CSV written with index=True) —
    not real data. Requires BOTH the name to look index-like AND the values to
    actually be sequential (_is_sequential_id_like_column) so a genuine data
    column that happens to be named 'index' but doesn't behave like one is
    never excluded on name alone.

    A raw leading index column like this makes every row look artificially
    unique to a plain df.duplicated() call, structurally hiding real duplicate
    rows (e.g. the reference notebook for this dataset loads with
    index_col="index", excluding it before comparing; this project's loader
    doesn't, so this column must be excluded from the comparison instead).
    """
    name_lower = name.strip().lower()
    name_matches = name_lower in _ROW_INDEX_NAME_HINTS or name_lower.startswith("unnamed:")
    return name_matches and _is_sequential_id_like_column(series)


def _check_duplicates(df: pd.DataFrame) -> list:
    issues = []
    index_like_cols = [c for c in df.columns if _looks_like_row_index_column(c, df[c])]
    compare_df = df.drop(columns=index_like_cols) if index_like_cols else df
    dup_row_count = compare_df.duplicated().sum()
    if dup_row_count:
        issues.append(f"Duplicate rows: {dup_row_count} fully duplicate rows found.")

    for col in df.columns:
        if not any(hint in col.lower() for hint in UNIQUE_ID_NAME_HINTS):
            continue
        non_null = df[col].dropna()
        if non_null.empty:
            continue
        dup_value_count = non_null.duplicated().sum()
        if dup_value_count:
            issues.append(
                f"Duplicate values: column '{col}' looks like a unique identifier but has "
                f"{dup_value_count} duplicate values."
            )
    return issues


def _check_dtypes(df: pd.DataFrame) -> list:
    """Numbers stored as text, and inconsistent date formats within the same column."""
    issues = []
    for col in df.columns:
        series = df[col].dropna().astype(str).str.strip()
        if series.empty:
            continue

        numeric_like = pd.to_numeric(series, errors="coerce")
        numeric_ratio = numeric_like.notna().mean()
        if 0 < numeric_ratio < 1.0 and numeric_ratio >= 0.8:
            issues.append(
                f"Wrong data type: column '{col}' is mostly numeric "
                f"({numeric_ratio:.0%} of values) but stored as text, with some "
                "non-numeric values mixed in."
            )

        date_like = pd.to_datetime(series, errors="coerce", format="mixed")
        date_ratio = date_like.notna().mean()
        if date_ratio >= 0.5:
            # Re-parse with each of a few common explicit formats to see whether every
            # parseable value actually matches a single consistent format, or whether
            # more than one distinct format is present (inconsistent date formats).
            candidate_formats = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y/%m/%d", "%m-%d-%Y"]
            matched_formats = set()
            for val in series:
                for fmt in candidate_formats:
                    try:
                        pd.to_datetime(val, format=fmt)
                        matched_formats.add(fmt)
                        break
                    except (ValueError, TypeError):
                        continue
            if len(matched_formats) > 1:
                issues.append(
                    f"Wrong data type: column '{col}' contains dates in more than one "
                    f"format ({sorted(matched_formats)})."
                )
    return issues


def _check_categorical_inconsistency(df: pd.DataFrame) -> list:
    """Same real value written different ways (case/whitespace variants collapsing to
    the same normalized form) within a column that looks categorical (low cardinality,
    text-typed)."""
    issues = []
    n = len(df)
    if n == 0:
        return issues
    for col in df.columns:
        series = df[col].dropna().astype(str)
        if series.empty:
            continue
        raw_unique = series.nunique()
        if raw_unique == 0:
            continue
        normalized = series.str.strip().str.lower()
        normalized_unique = normalized.nunique()
        # Only consider plausibly-categorical columns: after normalizing away
        # whitespace/case noise, there must still be real repetition (an id or
        # free-text column stays high-cardinality even after normalizing, so it's
        # excluded here rather than by raw cardinality alone, which a few accidental
        # case/whitespace variants could otherwise inflate).
        if normalized_unique / n > 0.5:
            continue
        if normalized_unique < raw_unique:
            issues.append(
                f"Inconsistent categorical values: column '{col}' has {raw_unique} distinct "
                f"raw values but only {normalized_unique} after trimming whitespace/lowercasing "
                "— the same value is written multiple different ways."
            )
    return issues


def _check_formatting_noise(df: pd.DataFrame) -> list:
    """Stray leading/trailing whitespace in text columns (separate from the categorical
    check above, which only fires on low-cardinality columns — this catches whitespace
    noise in any text column, including high-cardinality ones)."""
    issues = []
    for col in df.columns:
        series = df[col].dropna().astype(str)
        if series.empty:
            continue
        stripped_diff = (series != series.str.strip()).sum()
        if stripped_diff:
            issues.append(
                f"Formatting noise: column '{col}' has {stripped_diff} values with stray "
                "leading/trailing whitespace."
            )
    return issues


_PROSE_BULLET_CHARS = ("•", "◦", "▪", "‣", "●", "∙")


def _check_prose_structural_noise(df: pd.DataFrame) -> list:
    """Bullet characters and embedded newlines inside long-form prose columns
    (e.g. a free-text "Job Description" field). This is structurally invisible
    to every other static check (none target prose specifically) AND to
    discovery (_is_long_form_prose_column makes explore_column skip prose
    columns entirely — see its docstring), so without this check the pattern
    is never caught anywhere in the pipeline.

    Deliberately a fixed-pattern MECHANICAL check, not exploratory: bullets
    should become periods and embedded newlines should collapse to spaces —
    there's one clearly correct fix (mirroring what the reference notebook for
    this dataset does), so this belongs in the static warn-level rubric like
    _check_formatting_noise, not routed through the LLM-driven discovery pass.
    """
    issues = []
    for col in df.columns:
        series = df[col].dropna().astype(str)
        if series.empty or not _is_long_form_prose_column(df[col]):
            continue
        has_bullet = series.apply(lambda v: any(ch in v for ch in _PROSE_BULLET_CHARS))
        bullet_count = has_bullet.sum()
        newline_count = series.str.contains("\n").sum()
        if not bullet_count and not newline_count:
            continue
        parts = []
        if bullet_count:
            parts.append(f"{bullet_count} value(s) contain bullet characters")
        if newline_count:
            parts.append(f"{newline_count} value(s) contain embedded newlines")
        issues.append(
            f"Structural noise in prose: column '{col}' has " + " and ".join(parts) +
            " — bullet characters should become periods and embedded newlines should "
            "collapse to spaces."
        )
    return issues


def _check_impossible_values(df: pd.DataFrame) -> list:
    """Negative values in columns whose name suggests they should be non-negative
    counts/quantities/amounts, and out-of-range dates (before 1900 or far in the future)."""
    issues = []
    for col in df.columns:
        col_lower = col.lower()
        numeric_series = pd.to_numeric(df[col], errors="coerce").dropna()
        if numeric_series.empty:
            continue

        if any(hint in col_lower for hint in NON_NEGATIVE_NAME_HINTS):
            negative_count = (numeric_series < 0).sum()
            if negative_count:
                issues.append(
                    f"Invalid values: column '{col}' has {negative_count} negative "
                    "value(s) in a column that should not be negative."
                )

    for col in df.columns:
        col_lower = col.lower()
        if "date" not in col_lower and "time" not in col_lower:
            continue
        parsed = pd.to_datetime(df[col], errors="coerce", format="mixed")
        parsed = parsed.dropna()
        if parsed.empty:
            continue
        too_old = (parsed.dt.year < 1900).sum()
        too_future = (parsed.dt.year > pd.Timestamp.now().year + 1).sum()
        if too_old or too_future:
            issues.append(
                f"Invalid values: column '{col}' has {too_old} date(s) before 1900 and "
                f"{too_future} date(s) more than a year in the future — out-of-range dates."
            )
    return issues


# ---------------------------------------------------------------------------
# 17 additional deterministic checks, added later, same discipline as the original 8
# above: real logic, no LLM, naming heuristics rather than hardcoded per-dataset names.
# ---------------------------------------------------------------------------


def _check_placeholder_values(df: pd.DataFrame) -> list:
    """Placeholder tokens ("9999", "-1", "TBD", "Unknown", ...) standing in for a
    genuine value. Only fires when a placeholder token appears as a MINORITY of a
    column's values (< 50%) — a column that legitimately consists mostly of one such
    label (e.g. a real "status" column that's mostly "unknown") isn't a placeholder
    problem, it's a real category; a small number mixed in among otherwise normal
    values is the actual signal something is standing in for missing data."""
    issues = []
    for col in df.columns:
        series = df[col].dropna().astype(str).str.strip()
        if series.empty:
            continue
        normalized = series.str.lower()
        is_placeholder = normalized.isin(PLACEHOLDER_TOKENS)
        placeholder_count = is_placeholder.sum()
        if placeholder_count == 0:
            continue
        placeholder_frac = placeholder_count / len(series)
        if placeholder_frac < 0.5:
            found_tokens = sorted(normalized[is_placeholder].unique())
            issues.append(
                f"Placeholder values: column '{col}' has {placeholder_count} value(s) "
                f"that look like placeholders standing in for real data ({found_tokens}), "
                "mixed in among otherwise genuine values."
            )
    return issues


def _check_boolean_inconsistency(df: pd.DataFrame) -> list:
    """A column whose non-null values are drawn from more than one boolean-token
    "family" at once (e.g. some rows 'Y'/'N', others '1'/'0') — a genuine boolean
    column uses exactly one representation consistently."""
    issues = []
    for col in df.columns:
        series = df[col].dropna().astype(str).str.strip().str.lower()
        if series.empty:
            continue
        distinct = set(series.unique())
        if not (1 < len(distinct) <= 4):
            continue
        all_tokens = set().union(*BOOLEAN_FAMILIES.values())
        if not distinct.issubset(all_tokens):
            continue
        families_used = [name for name, toks in BOOLEAN_FAMILIES.items() if distinct & toks]
        if len(families_used) > 1:
            issues.append(
                f"Inconsistent boolean representations: column '{col}' mixes more than "
                f"one boolean style at once ({sorted(families_used)}), values seen: "
                f"{sorted(distinct)}."
            )
    return issues


def _check_lost_leading_zeros(df: pd.DataFrame) -> list:
    """A column that looks like it should preserve leading zeros (zip/postal code by
    name) where the most common value length is longer than some other values —
    consistent with a leading zero having been dropped somewhere upstream."""
    issues = []
    for col in df.columns:
        if not any(hint in col.lower() for hint in ZIP_NAME_HINTS):
            continue
        series = df[col].dropna().astype(str).str.strip()
        digit_only = series[series.str.fullmatch(r"\d+")]
        if len(digit_only) < 2:
            continue
        lengths = digit_only.str.len()
        mode_len = lengths.mode().iloc[0]
        short_count = (lengths == mode_len - 1).sum()
        if short_count:
            issues.append(
                f"Lost leading zeros: column '{col}' has {short_count} value(s) one "
                f"digit shorter than the common length ({mode_len}) — likely a dropped "
                "leading zero."
            )
    return issues


def _check_currency_unit_symbols(df: pd.DataFrame) -> list:
    """Currency symbols or unit suffixes embedded in what is otherwise a numeric-looking
    column (e.g. '$50.00', '50 kg' stored as text instead of a plain number).

    The specific symbols/suffixes actually found are named in the issue text (e.g.
    "(['$'])" or "(['kg'])") — this is what lets _extract_currency_unit_symbols
    (see the treatment-signature section below) tell apart two columns that
    genuinely need the identical mechanical strip (same symbols/suffixes) from two
    that merely share this category but need different literal characters removed.
    """
    issues = []
    currency_pattern = re.compile(rf"^[{re.escape(CURRENCY_SYMBOLS)}]\s?\d")
    unit_pattern = re.compile(
        r"^\d+(\.\d+)?\s?(" + "|".join(re.escape(u) for u in UNIT_SUFFIXES) + r")$",
        re.IGNORECASE,
    )
    for col in df.columns:
        series = df[col].dropna().astype(str).str.strip()
        if series.empty:
            continue
        currency_matches = series.str.contains(currency_pattern)
        unit_matches = series.str.match(unit_pattern)
        total_matches = (currency_matches | unit_matches).sum()
        if total_matches:
            found_symbols: set = set()
            for val in series[currency_matches]:
                for sym in CURRENCY_SYMBOLS:
                    if sym in val:
                        found_symbols.add(sym)
            for val in series[unit_matches]:
                unit_match = unit_pattern.match(val)
                if unit_match:
                    found_symbols.add(unit_match.group(2).lower())
            issues.append(
                f"Currency/unit symbols: column '{col}' has {total_matches} value(s) with "
                f"a currency symbol or unit suffix embedded in an otherwise numeric value "
                f"({sorted(found_symbols)})."
            )
    return issues


def _check_locale_number_formatting(df: pd.DataFrame) -> list:
    """Locale-specific number formatting mixed within one column: some values styled
    '1,234.56' (comma thousands / period decimal) and others '1.234,56' (period
    thousands / comma decimal) in the same column."""
    us_pattern = re.compile(r"^\d{1,3}(,\d{3})+\.\d+$")
    eu_pattern = re.compile(r"^\d{1,3}(\.\d{3})+,\d+$")
    issues = []
    for col in df.columns:
        series = df[col].dropna().astype(str).str.strip()
        if series.empty:
            continue
        us_count = series.str.match(us_pattern).sum()
        eu_count = series.str.match(eu_pattern).sum()
        if us_count and eu_count:
            issues.append(
                f"Locale-specific number formatting: column '{col}' mixes US-style "
                f"({us_count} value(s), e.g. 1,234.56) and EU-style ({eu_count} value(s), "
                "e.g. 1.234,56) number formatting."
            )
    return issues


def _check_float_noise(df: pd.DataFrame) -> list:
    """Excessive floating-point noise: a value like 19.989999999999998 that looks like
    an upstream floating-point computation artifact rather than a genuinely entered
    number (a long run of repeated trailing 9s or 0s right after the decimal point)."""
    noise_pattern = re.compile(r"\.\d*([09])\1{4,}\d?$")
    issues = []
    for col in df.columns:
        series = df[col].dropna().astype(str).str.strip()
        if series.empty:
            continue
        matches = series.map(lambda v: bool(noise_pattern.search(v)))
        match_count = matches.sum()
        if match_count:
            issues.append(
                f"Excessive floating-point noise: column '{col}' has {match_count} "
                "value(s) with long runs of repeated trailing digits (e.g. "
                "19.989999999999998), suggesting an upstream computation artifact."
            )
    return issues


def _check_control_characters(raw_text: str) -> list:
    """Non-printable/control characters embedded in text fields (excludes plain
    tab/newline/carriage-return, which pandas' CSV parsing itself relies on).

    Operates on the raw decoded text via csv.reader rather than the parsed DataFrame:
    pandas' C parser treats an embedded NUL byte as a C-string terminator and silently
    truncates the field before it (e.g. 'Bob\\x00Smith' becomes just 'Bob'), which would
    hide exactly the kind of corruption this check exists to catch."""
    control_pattern = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
    issues = []
    reader = csv.reader(raw_text.splitlines())
    try:
        header = next(reader)
    except StopIteration:
        return issues
    counts = [0] * len(header)
    for row in reader:
        for i, val in enumerate(row):
            if i < len(counts) and control_pattern.search(val):
                counts[i] += 1
    for col, count in zip(header, counts):
        if count:
            issues.append(
                f"Non-printable characters: column '{col}' has {count} value(s) "
                "containing control/non-printable characters."
            )
    return issues


def _check_spreadsheet_artifacts(df: pd.DataFrame) -> list:
    """Copy-paste artifacts from spreadsheet tools: a literal formula string
    ('=SUM(A1:A2)') or an Excel error string ('#DIV/0!', '#REF!', ...) landing in the
    data instead of a computed/real value."""
    formula_pattern = re.compile(r"^=\s*[A-Za-z]+\s*\(")
    issues = []
    for col in df.columns:
        series = df[col].dropna().astype(str).str.strip()
        if series.empty:
            continue
        formula_matches = series.str.match(formula_pattern).sum()
        error_matches = series.isin(EXCEL_ERROR_TOKENS).sum()
        if formula_matches:
            issues.append(
                f"Spreadsheet artifacts: column '{col}' has {formula_matches} value(s) "
                "that look like a literal, uncalculated spreadsheet formula (e.g. '=SUM(...)')."
            )
        if error_matches:
            issues.append(
                f"Spreadsheet artifacts: column '{col}' has {error_matches} value(s) "
                "that are literal Excel error strings (e.g. '#DIV/0!', '#REF!')."
            )
    return issues


def _looks_like_delimited_list(value: str, delim: str) -> bool:
    """True if splitting value on delim produces 2+ short, simple, non-empty tokens —
    a proxy for 'this looks like a deliberate multi-value list', as opposed to a
    comma appearing incidentally inside ordinary prose (e.g. 'Smith, John')."""
    parts = value.split(delim)
    if len(parts) < 2:
        return False
    return all(0 < len(p.strip()) <= 30 and re.fullmatch(r"[\w \-]+", p.strip()) for p in parts)


def _check_inconsistent_delimiters(df: pd.DataFrame) -> list:
    """A column that holds multi-value fields (a list of items in one cell) where the
    separator character isn't consistent across rows — some rows comma-separated, some
    semicolon, some pipe, for what looks like the same kind of field."""
    delims = (",", ";", "|")
    issues = []
    for col in df.columns:
        series = df[col].dropna().astype(str)
        if series.empty:
            continue
        delims_used = set()
        for val in series:
            for d in delims:
                if d in val and _looks_like_delimited_list(val, d):
                    delims_used.add(d)
        if len(delims_used) > 1:
            issues.append(
                f"Inconsistent delimiters: column '{col}' uses more than one separator "
                f"({sorted(delims_used)}) for what looks like the same kind of "
                "multi-value field."
            )
    return issues


def _read_header_row(raw_text: str) -> list:
    """Real header row exactly as written in the file (not pandas' post-processed
    df.columns, which silently renames blank/duplicate headers) — needed by the
    header-focused checks below."""
    reader = csv.reader(raw_text.splitlines())
    try:
        return next(reader)
    except StopIteration:
        return []


def _check_header_issues(raw_text: str) -> list:
    """Whitespace or inconsistent casing in header names, or duplicate header names —
    checked against the real header row as written, before pandas mangles duplicates
    by suffixing them (e.g. 'id', 'id.1')."""
    issues = []
    headers = _read_header_row(raw_text)
    if not headers:
        return issues

    whitespace_headers = [h for h in headers if h != h.strip()]
    if whitespace_headers:
        issues.append(
            f"Column header issues: {len(whitespace_headers)} header(s) have leading/"
            f"trailing whitespace ({whitespace_headers})."
        )

    normalized = [h.strip().lower() for h in headers]
    seen = set()
    dupes = set()
    for h in normalized:
        if h in seen:
            dupes.add(h)
        seen.add(h)
    if dupes:
        issues.append(f"Column header issues: duplicate column name(s) found: {sorted(dupes)}.")

    def _style(h: str) -> str:
        stripped = h.strip()
        if not stripped or not stripped.isalpha() and "_" not in stripped and " " not in stripped:
            return "other"
        if stripped.isupper():
            return "upper"
        if stripped.islower() or "_" in stripped:
            return "snake_or_lower"
        if stripped[0].isupper():
            return "title_or_camel"
        return "other"

    styles = {_style(h) for h in headers if _style(h) != "other"}
    if len(styles) > 1:
        issues.append(
            f"Column header issues: header names mix inconsistent casing conventions "
            f"({sorted(styles)}) across {headers}."
        )
    return issues


def _check_special_chars_in_headers(raw_text: str) -> list:
    """Special characters in column names (anything beyond letters/digits/underscore/
    space/hyphen) that would break downstream SQL/tooling that expects plain
    identifiers."""
    issues = []
    headers = _read_header_row(raw_text)
    bad_headers = [h for h in headers if re.search(r"[^\w \-]", h)]
    if bad_headers:
        issues.append(
            f"Special characters in headers: {len(bad_headers)} header(s) contain "
            f"characters that would break downstream tooling ({bad_headers})."
        )
    return issues


def _check_column_misalignment_quotes(raw_text: str) -> list:
    """Column misalignment caused specifically by unmatched quote characters on a
    single physical line — distinct from the existing ragged-column-count structural
    check, since a stray unmatched quote can cause a parser to silently merge or split
    rows even when the resulting field count happens to still match the header."""
    issues = []
    lines = raw_text.splitlines()[1:]  # skip header
    bad_lines = sum(1 for line in lines if line.count('"') % 2 != 0)
    if bad_lines:
        issues.append(
            f"Column misalignment: {bad_lines} row(s) contain an unmatched quote "
            "character, which can cause fields to be misread or misaligned."
        )
    return issues


def _check_header_duplicated_mid_file(raw_text: str) -> list:
    """The header row appears again, verbatim, somewhere in the data — a common
    artifact of concatenating multiple exports of the same file together."""
    issues = []
    reader = csv.reader(raw_text.splitlines())
    try:
        header = next(reader)
    except StopIteration:
        return issues
    dup_count = sum(1 for row in reader if row == header)
    if dup_count:
        issues.append(
            f"Header row duplicated mid-file: the header row appears again verbatim "
            f"{dup_count} time(s) among the data rows."
        )
    return issues


def _check_trailing_empty(df: pd.DataFrame, raw_text: str) -> list:
    """Trailing empty rows at the end of the file, or a trailing column that's entirely
    empty (either a blank/unnamed header, or a fully-null column)."""
    issues = []
    lines = [line for line in raw_text.splitlines()]
    data_lines = lines[1:] if lines else []
    trailing_empty_rows = 0
    for line in reversed(data_lines):
        if line.strip() == "" or set(line.strip()) <= {","}:
            trailing_empty_rows += 1
        else:
            break
    if trailing_empty_rows:
        issues.append(f"Trailing empty rows: {trailing_empty_rows} blank row(s) at the end of the file.")

    if len(df.columns) > 0:
        last_col = str(df.columns[-1])
        looks_unnamed = last_col.strip() == "" or last_col.startswith("Unnamed:")
        if looks_unnamed and bool(df[last_col].isna().all()):
            issues.append(f"Trailing empty column: last column ('{last_col}') is entirely empty.")
    return issues


def _check_bom(raw_bytes: bytes) -> list:
    """A byte-order-mark at the start of the file, which corrupts the first column
    name when read as plain UTF-8 (it ends up prefixed with a stray \\ufeff character)."""
    issues = []
    if raw_bytes.startswith(b"\xef\xbb\xbf"):
        issues.append(
            "Byte-order-mark: file starts with a UTF-8 BOM, which will corrupt the "
            "first column name unless explicitly stripped."
        )
    return issues


def _check_granularity_inconsistency(df: pd.DataFrame) -> list:
    """A datetime column where some values are recorded at day-level precision and
    others down to the second (or minute) within the same column."""
    issues = []
    for col in df.columns:
        col_lower = col.lower()
        if "date" not in col_lower and "time" not in col_lower:
            continue
        series = df[col].dropna().astype(str).str.strip()
        parsed = pd.to_datetime(series, errors="coerce", format="mixed")
        valid = series[parsed.notna()]
        if len(valid) < 2:
            continue
        has_time_component = valid.str.contains(r"\d{1,2}:\d{2}")
        day_only_count = (~has_time_component).sum()
        with_time_count = has_time_component.sum()
        if day_only_count and with_time_count:
            issues.append(
                f"Inconsistent granularity: column '{col}' mixes day-level values "
                f"({day_only_count}) with time-of-day values ({with_time_count}) in "
                "the same column."
            )
    return issues


def _check_dangling_references(df: pd.DataFrame) -> list:
    """A column that looks like a self-referencing identifier (name suggests it points
    at another row in this same file, e.g. 'parent_id', 'manager_id') where some values
    don't correspond to anything in this file's own primary identifier column. Only
    checked when a clear primary-identifier candidate column ('id', case-insensitive)
    actually exists in the file — otherwise there's nothing to check the references
    against, and this is silently skipped rather than guessed at."""
    issues = []
    id_cols = [c for c in df.columns if c.strip().lower() == "id"]
    if not id_cols:
        return issues
    primary_col = id_cols[0]
    valid_ids = set(df[primary_col].dropna().astype(str).str.strip())
    if not valid_ids:
        return issues

    for col in df.columns:
        if col == primary_col:
            continue
        col_lower = col.lower()
        if not col_lower.endswith("id") or col_lower == "id":
            continue
        values = df[col].dropna().astype(str).str.strip()
        if values.empty:
            continue
        dangling = values[~values.isin(valid_ids)]
        if len(dangling):
            issues.append(
                f"Dangling references: column '{col}' looks like a reference to "
                f"'{primary_col}' but has {len(dangling)} value(s) not found there."
            )
    return issues


def check_rubric(file_path) -> list:
    """Run the full rubric against a single raw file. Returns a list of human-readable
    issue strings; an empty list means nothing was flagged for this file.

    Reads the file both as raw bytes (for encoding/structural checks, which need to see
    the file before any parser normalizes it) and via pandas (for the content-level
    checks, which need real column values). A file that fails to parse as CSV at all is
    reported as a structural issue rather than raising.
    """
    path = Path(file_path)
    issues: list = []

    raw_bytes = _read_raw_bytes(path)
    raw_text = raw_bytes.decode("utf-8", errors="replace")
    issues += _check_encoding(raw_bytes)
    issues += _check_structural(path, raw_bytes)
    issues += _check_bom(raw_bytes)
    issues += _check_header_issues(raw_text)
    issues += _check_special_chars_in_headers(raw_text)
    issues += _check_column_misalignment_quotes(raw_text)
    issues += _check_header_duplicated_mid_file(raw_text)
    issues += _check_control_characters(raw_text)

    try:
        df = _read_csv_robust(path)
    except Exception as e:
        issues.append(f"Structural issue: file could not be parsed as CSV at all: {e}")
        return issues

    issues += _check_missing_values(df)
    issues += _check_duplicates(df)
    issues += _check_dtypes(df)
    issues += _check_categorical_inconsistency(df)
    issues += _check_formatting_noise(df)
    issues += _check_prose_structural_noise(df)
    issues += _check_impossible_values(df)
    issues += _check_placeholder_values(df)
    issues += _check_boolean_inconsistency(df)
    issues += _check_lost_leading_zeros(df)
    issues += _check_currency_unit_symbols(df)
    issues += _check_locale_number_formatting(df)
    issues += _check_float_noise(df)
    issues += _check_spreadsheet_artifacts(df)
    issues += _check_inconsistent_delimiters(df)
    issues += _check_trailing_empty(df, raw_text)
    issues += _check_granularity_inconsistency(df)
    issues += _check_dangling_references(df)

    return issues


# ---------------------------------------------------------------------------
# Piece 1a: treatment signatures for batching fail/warn-level issues that require
# IDENTICAL fixes across different columns (Spec 4).
#
# check_rubric()'s checks run per-column, so a dataset with the same underlying
# problem in several columns (e.g. "-1" placeholder tokens in eight different
# columns) produces eight separate issue strings. clean_dataset()'s fail-level
# loop processes every fail-level issue strictly one at a time — its own LLM
# call, its own approval prompt — even when the correct fix is identical logic
# applied to a different column name.
#
# Two issues can only be batched together when a MECHANICALLY VERIFIED signature
# proves their required treatment is identical — never an LLM's guess that two
# issues "seem similar," and never for a category where "identical" isn't
# actually well-defined yet (e.g. missing values legitimately needs a different
# strategy above/below a 20% threshold, even within the same category — blurring
# that into one LLM call risks silently applying the wrong strategy with no way
# to detect it afterward). Categories with no signature defined below always
# return None here and stay on the existing one-at-a-time path, unchanged.
# ---------------------------------------------------------------------------

_BRACKETED_QUOTED_LIST_RE = re.compile(r"\(\[(.+?)\]\)")


def _extract_bracketed_quoted_list(issue: str) -> frozenset:
    """Shared helper: extract a Python-list-repr-style bracketed, quoted token
    list embedded in an issue string as "(['a', 'b'])" — the exact shape both
    _check_placeholder_values (its found_tokens) and _check_boolean_inconsistency
    (its families_used) already produce via an f-string embedding
    `{sorted(some_list)}`. Matches only the FIRST such bracketed group in the
    issue text (non-greedy), which is what both of those checks' message
    formats place first.
    """
    match = _BRACKETED_QUOTED_LIST_RE.search(issue)
    if not match:
        return frozenset()
    return frozenset(re.findall(r"'([^']*)'", match.group(1)))


def _extract_placeholder_tokens(issue: str) -> frozenset:
    """The exact placeholder tokens (e.g. {'-1', 'unknown'}) named in a
    "Placeholder values: ..." issue string — same token vocabulary
    _check_placeholder_values / report.py's _parse_placeholder_issue already
    parse, extracted here as a frozenset for signature comparison."""
    return _extract_bracketed_quoted_list(issue)


def _extract_boolean_families(issue: str) -> frozenset:
    """The exact conflicting boolean-token families (e.g. {'y/n', '1/0'}) named
    in an "Inconsistent boolean representations: ..." issue string."""
    return _extract_bracketed_quoted_list(issue)


_CURRENCY_UNIT_SYMBOLS_RE = re.compile(r"embedded in an otherwise numeric value \(\[(.+?)\]\)")


def _extract_currency_unit_symbols(issue: str) -> frozenset:
    """The exact currency symbols/unit suffixes (e.g. {'$'}, {'kg'}) named in a
    "Currency/unit symbols: ..." issue string (see _check_currency_unit_symbols,
    which embeds them specifically so this extraction is possible)."""
    match = _CURRENCY_UNIT_SYMBOLS_RE.search(issue)
    if not match:
        return frozenset()
    return frozenset(re.findall(r"'([^']*)'", match.group(1)))


def _extract_locale_format_pair(issue: str) -> frozenset:
    """The exact pair of locale format labels (currently always {'US-style',
    'EU-style'} — the only two this project's _check_locale_number_formatting
    knows about) named in a "Locale-specific number formatting: ..." issue."""
    found = set()
    if "US-style" in issue:
        found.add("US-style")
    if "EU-style" in issue:
        found.add("EU-style")
    return frozenset(found)


_LEADING_ZERO_LENGTH_RE = re.compile(r"common length \((\d+)\)")


def _extract_leading_zero_target_length(issue: str) -> "int | None":
    """The common/target digit length (e.g. 5 for a 5-digit zip code) named in
    a "Lost leading zeros: ..." issue string."""
    match = _LEADING_ZERO_LENGTH_RE.search(issue)
    return int(match.group(1)) if match else None


def _extract_spreadsheet_artifact_kind(issue: str) -> "str | None":
    """"formula" or "excel_error" — the two distinct sub-kinds
    _check_spreadsheet_artifacts' message text already distinguishes; a
    literal-formula issue and an Excel-error-token issue need different fix
    logic even within the same "Spreadsheet artifacts:" category, so they must
    never share a signature."""
    if "uncalculated spreadsheet formula" in issue:
        return "formula"
    if "literal Excel error strings" in issue:
        return "excel_error"
    return None


def _column_value_type_category(series: pd.Series) -> str:
    """Coarse general value-type classification for a column's real non-null
    values, used to keep batching (see _issue_treatment_signature below) from
    grouping a numeric column together with a text column that merely happens
    to share the same placeholder/symbol tokens. Every column in this project
    is read with dtype=str (see _read_csv_robust), so pandas' own dtype is
    always "object" and can't distinguish these — this infers the real value
    shape from content instead.

    Returns "numeric" when at least _NUMERIC_TYPE_CATEGORY_THRESHOLD of real
    values parse as numbers, else "text". Two categories, not more: the one
    real, observed batching bug this fixes (Rating batched with Headquarters/
    Founded) is a numeric/text mismatch, not a finer-grained type distinction.
    """
    non_null = series.dropna().astype(str).str.strip()
    non_null = non_null[non_null != ""]
    if non_null.empty:
        return "text"
    numeric = pd.to_numeric(non_null, errors="coerce")
    numeric_frac = numeric.notna().mean()
    return "numeric" if numeric_frac >= _NUMERIC_TYPE_CATEGORY_THRESHOLD else "text"


_NUMERIC_TYPE_CATEGORY_THRESHOLD = 0.8


def _issue_treatment_signature(issue: str, df: "pd.DataFrame | None" = None) -> "tuple | None":
    """Return a hashable signature identifying the exact fix treatment this
    issue requires, or None if this issue type has no defined signature
    (meaning: never group it — always process alone). Two issues with equal,
    non-None signatures are guaranteed to require the identical fix operation,
    not just a similar-looking one.

    Defined for exactly six categories (see this section's module comment for
    why these six and not others): "Placeholder values:", "Currency/unit
    symbols:", "Spreadsheet artifacts:", "Locale-specific number formatting:",
    "Inconsistent boolean representations:", "Lost leading zeros:". Every other
    category — including missing values, wrong data types, duplicate rows/
    values, invalid values, dangling references, inconsistent categorical
    values, inconsistent delimiters, inconsistent granularity, formatting
    noise, control characters, and the discovery-phase "(discovered)" issues —
    returns None here and always stays on the one-at-a-time path.

    dtype-aware (safety fix): when df is given, the flagged column's real
    value-type category (_column_value_type_category — "numeric" or "text")
    is appended to the signature, so two issues only batch together when both
    the tokens AND the general value type genuinely match — otherwise a
    numeric column (e.g. Rating) could batch with a text column (e.g.
    Headquarters) purely by coincidence of sharing a token set (e.g. both
    using "-1" as a placeholder), producing a type-mismatched fix. df is
    optional and defaults to None (no dtype narrowing) so direct signature-only
    callers/tests that only care about token extraction, not batching safety,
    are unaffected.
    """
    base = None
    if issue.startswith("Placeholder values:"):
        tokens = _extract_placeholder_tokens(issue)
        base = ("placeholder", tokens) if tokens else None

    elif issue.startswith("Currency/unit symbols:"):
        symbols = _extract_currency_unit_symbols(issue)
        base = ("currency_unit", symbols) if symbols else None

    elif issue.startswith("Spreadsheet artifacts:"):
        kind = _extract_spreadsheet_artifact_kind(issue)
        base = ("spreadsheet_artifact", kind) if kind is not None else None

    elif issue.startswith("Locale-specific number formatting:"):
        pair = _extract_locale_format_pair(issue)
        base = ("locale_format", pair) if pair else None

    elif issue.startswith("Inconsistent boolean representations:"):
        families = _extract_boolean_families(issue)
        base = ("boolean_families", families) if families else None

    elif issue.startswith("Lost leading zeros:"):
        length = _extract_leading_zero_target_length(issue)
        base = ("leading_zeros", length) if length is not None else None

    if base is None or df is None:
        return base

    col_match = _ISSUE_COLUMN_RE.search(issue)
    if not col_match or col_match.group(1) not in df.columns:
        return base

    dtype_category = _column_value_type_category(df[col_match.group(1)])
    return base + (dtype_category,)


def _group_issues_by_signature(issues: list, df: "pd.DataFrame | None" = None) -> list:
    """Partition issues into groups for processing. Issues whose
    _issue_treatment_signature matches (non-None and equal) are grouped
    together (2+ issues per group); every issue with signature None, or with a
    signature unique among this file's issues, becomes its own single-item
    group. Order of groups follows the order the first issue in each group was
    originally found — preserves check_rubric's original ordering for anything
    not grouped.

    df: optional, forwarded to _issue_treatment_signature so grouping is
    dtype-aware (see its docstring) — pass the file's real DataFrame to
    prevent a numeric column batching with a text column that merely shares
    the same tokens. Defaults to None (no dtype narrowing) for direct callers
    that only need the pre-existing token-based grouping.
    """
    groups: dict = {}
    order: list = []

    for issue in issues:
        sig = _issue_treatment_signature(issue, df=df)
        if sig is None:
            order.append(("single", issue))
            continue
        if sig not in groups:
            groups[sig] = []
            order.append(("group", sig))
        groups[sig].append(issue)

    result = []
    for kind, value in order:
        if kind == "single":
            result.append([value])
        else:
            result.append(groups[value])
    return result


def _partition_warn_groups(warn_issues: list, df: "pd.DataFrame | None" = None) -> list:
    """Warn-level counterpart to the fail-level grouping above, but with one
    deliberate difference: warn-level issues have ALWAYS been processed as one
    single combined batch (unlike fail-level, which was always one-at-a-time) —
    Spec 4 only carves real structure OUT of that existing default, it doesn't
    replace it. So: any _group_issues_by_signature group with 2+ issues (a
    genuine, mechanically-verified shared treatment) becomes its own batch: but
    every group of size 1 — signature None, or a signature that happened not to
    match any other warn issue in this file — is pooled back together into ONE
    final combined batch, preserving the original "always one whole-file warn
    batch" behavior for anything that isn't a genuine multi-issue signature
    match. Returns [] for an empty input (no warn issues at all).

    df: optional, forwarded to _group_issues_by_signature for dtype-aware
    grouping — see its docstring.
    """
    if not warn_issues:
        return []
    raw_groups = _group_issues_by_signature(warn_issues, df=df)
    real_batches = [g for g in raw_groups if len(g) >= 2]
    leftover = [issue for g in raw_groups if len(g) < 2 for issue in g]
    return real_batches + [leftover] if leftover else real_batches


# ---------------------------------------------------------------------------
# Piece 1b: LLM-driven exploratory discovery, mechanically verified before it's
# ever trusted. check_rubric() above is a closed catalog of pattern-matchers — it
# only catches problem shapes someone anticipated in advance. The functions below
# add a "glance and notice" pass that mimics a human running
# sorted(df[col].unique()) and going "huh, that's weird" — but nothing an LLM
# notices here is allowed to become a real issue string on its own word. Every
# hypothesis is converted into a deterministic, mechanical test against the real
# full column (never just the sample) before it's accepted — same discipline
# already used elsewhere in this codebase (agents/sql_analyst.py's rubric
# disclosures are always mechanically extracted from real executed SQL, never
# taken on an LLM's word), applied here to exploration instead of query
# generation.
# ---------------------------------------------------------------------------

_ISSUE_COLUMN_RE = re.compile(r"column '([^']+)'")


def _columns_with_fail_issues(issues: list) -> set:
    """Column names mentioned in any FAIL-level issue string from check_rubric().

    Used to skip explore_column for a column check_rubric already flagged as
    needing a fix this run — no benefit to discovering more on top of a column
    already known to need fixing. Extracted directly from the issue text (every
    per-column check in check_rubric formats its message as
    "... column '<name>' ..."), not a hardcoded list of category names.
    """
    flagged = set()
    for issue in issues:
        if _issue_severity(issue) != "fail":
            continue
        flagged.update(_ISSUE_COLUMN_RE.findall(issue))
    return flagged


def _is_long_form_prose_column(series: pd.Series) -> bool:
    """True when a column's real values average longer than
    _EXPLORE_PROSE_MEAN_LEN_THRESHOLD characters — a proxy for free-text prose
    (e.g. a "Job Description" column) where "does this hold two variables glued
    together" isn't a meaningful question. Used to skip explore_column entirely
    for such columns, avoiding a wasted LLM call."""
    non_null = series.dropna()
    if non_null.empty:
        return False
    mean_len = non_null.astype(str).str.len().mean()
    return bool(mean_len is not None and mean_len > _EXPLORE_PROSE_MEAN_LEN_THRESHOLD)


# Fraction of a column's real values that must fall inside the "ideal" contiguous
# integer range (0..n-1 or 1..n) for _is_sequential_id_like_column to treat it as a
# row identifier rather than real data — allows some tolerance for gaps left by
# earlier row filtering, without being so loose it also swallows genuine data.
_SEQUENTIAL_ID_RANGE_OVERLAP_THRESHOLD = 0.9


def _is_sequential_id_like_column(series: pd.Series) -> bool:
    """True if a column looks like a plain sequential/row identifier rather than
    real data worth exploring for composite structure — e.g. all-numeric, strictly
    monotonic, or a near-permutation of a small integer range roughly matching the
    row count (0..n-1 or 1..n, some tolerance for gaps from earlier row filtering).

    A plain sequential id is the most common false-positive shape discovery hits:
    it trivially satisfies almost any narrow all-digit regex at or near 100%, so a
    "Composite field (discovered)" hypothesis on a column like this is essentially
    guaranteed to mechanically verify despite there being no real second value
    glued to anything. This is a cheap, deterministic PRECISION improvement for
    that specific, common shape — heuristic, not exhaustive, and deliberately NOT
    a replacement for the NO_SPLIT decline sentinel in
    _generate_composite_split_code, which remains the general-purpose safety net
    for false-positive shapes this heuristic doesn't catch.
    """
    non_null = series.dropna()
    n = len(non_null)
    if n < 2:
        return False

    numeric = pd.to_numeric(non_null, errors="coerce")
    if numeric.isna().any():
        return False
    values = numeric.tolist()

    is_increasing = all(values[i] < values[i + 1] for i in range(n - 1))
    is_decreasing = all(values[i] > values[i + 1] for i in range(n - 1))
    if is_increasing or is_decreasing:
        return True

    if not all(float(v).is_integer() for v in values):
        return False
    int_values = {int(v) for v in values}
    for start in (0, 1):
        ideal_range = set(range(start, start + n))
        overlap_frac = len(int_values & ideal_range) / n
        if overlap_frac >= _SEQUENTIAL_ID_RANGE_OVERLAP_THRESHOLD:
            return True
    return False


def _sample_column_values(non_null: pd.Series) -> list:
    """Sample up to EXPLORE_SAMPLE_SIZE real values from a column, mixing a plain
    random sample with the column's least-frequent (rarest) distinct values.

    This mimics the part of human scanning that actually surfaces a one-off
    oddity: pure-random sampling would almost never draw a mostly-unique value
    like "Healthfirst\\n3.1" out of a column with hundreds of rows, but sorting
    by real frequency and taking the rarest values puts it directly in view.
    """
    n = len(non_null)
    if n == 0:
        return []
    sample_size = min(EXPLORE_SAMPLE_SIZE, n)
    rare_target = int(round(sample_size * EXPLORE_RARE_VALUE_FRACTION))
    random_target = sample_size - rare_target

    value_counts = non_null.value_counts().sort_values(ascending=True)
    rare_values = list(value_counts.index[:rare_target])

    random_target = min(random_target, n)
    random_sample = non_null.sample(n=random_target).tolist() if random_target > 0 else []

    combined = random_sample + rare_values
    return combined[:sample_size]


EXPLORE_COLUMN_SYSTEM_PROMPT = """You are looking at real sampled values from one column
of a dataset. Do not fix anything. Do not assume the column's name tells you what it
contains — judge only from the actual values shown.

Describe anything about these values that looks unusual, inconsistent, or that suggests
the column might actually contain more than one distinct piece of information glued
together. If nothing looks unusual, say so plainly — do not invent a finding to have
something to report.

For each thing you notice, state it as a testable hypothesis about the FULL column,
not just the sample — e.g. "this column may hold two variables joined by a newline,
with the second part looking like a decimal number" rather than "row 4 looks weird."
"""


def explore_column(df: pd.DataFrame, column: str, llm=None) -> list:
    """Open-ended, per-column "glance and notice" pass. Returns a list of loose,
    plain-English hypothesis strings — NEVER something check_rubric() would
    accept directly as an issue. An empty list is a valid, expected result; most
    columns should produce nothing.

    Skips the LLM call entirely (returns []) for a column that looks like
    long-form prose (see _is_long_form_prose_column), looks like a plain
    sequential row identifier (see _is_sequential_id_like_column — the single
    most common false-positive shape discovery hits, since a sequential id
    trivially matches almost any narrow all-digit regex), has no non-null
    values at all, or has too few real values to draw any real conclusion from
    (see _EXPLORE_MIN_COLUMN_ROWS — a tiny column makes any regex a tautology,
    not evidence). The other skip condition from the spec — a column
    check_rubric already flagged with a fail-level issue this run — is applied
    by the caller (explore_and_verify), which is where that information
    actually lives.
    """
    series = df[column]
    non_null = series.dropna()
    if (
        len(non_null) < _EXPLORE_MIN_COLUMN_ROWS
        or non_null.empty
        or _is_long_form_prose_column(series)
        or _is_sequential_id_like_column(series)
    ):
        return []

    if llm is None:
        from utils.llm_pick import pick_llm
        llm = pick_llm("cheap")

    sample_values = _sample_column_values(non_null)
    human_content = (
        f"Column name: {column}\n"
        f"Inferred dtype: {series.dtype}\n"
        f"Cardinality: {non_null.nunique()} distinct value(s) out of {len(df)} row(s) "
        f"({len(non_null)} non-null)\n\n"
        "Sampled real values (mix of random + rare/low-frequency):\n"
        + "\n".join(f"  - {v!r}" for v in sample_values)
    )

    try:
        structured_llm = llm.with_structured_output(ExplorationHypothesis)
        result: ExplorationHypothesis = structured_llm.invoke(
            [
                ("system", EXPLORE_COLUMN_SYSTEM_PROMPT),
                ("human", human_content),
            ]
        )
    except Exception:
        # An exploration call is best-effort noticing, not a required step —
        # an LLM error here just means "nothing noticed this time", never a
        # reason to abort the rest of discovery or cleaning.
        return []

    return list(result.hypotheses)


VERIFY_HYPOTHESIS_SYSTEM_PROMPT = """You are given a testable hypothesis about a real
dataset column, proposed during an earlier exploratory pass, along with that column's
real dtype.

Turn this hypothesis into something mechanically checkable: a single regular expression
(Python re syntax) that would match a value in this column if the hypothesis is
genuinely true, and a match_threshold — the minimum fraction (0.0-1.0) of the column's
real non-null values that should match this pattern for the hypothesis to count as
confirmed rather than a one-off coincidence.

If the hypothesis genuinely cannot be expressed as a single regex + threshold pair,
still return your best attempt — the caller mechanically re-checks it against every
real value in the column and discards it if the match rate doesn't hold up, so an
overly loose or slightly wrong pattern is safely caught downstream, not something you
need to get perfectly right here."""


def _verify_hypothesis(df: pd.DataFrame, column: str, hypothesis: str, llm=None) -> tuple:
    """The trust boundary: converts one loose hypothesis into a concrete regex +
    threshold via the LLM, then checks that regex against EVERY non-null value in
    the real column (not the sample) with plain Python/pandas — no LLM involved
    in the actual verification step.

    Returns (None, None) (hypothesis discarded, never escalated) when:
    - the column has too few real values to meaningfully verify against (see
      _EXPLORE_MIN_COLUMN_ROWS — defense in depth alongside explore_column's
      own skip, in case this is ever called directly with a hypothesis from
      elsewhere);
    - the LLM call fails, or its proposed pattern doesn't compile as a regex;
    - the real match fraction against the full column is below the proposed
      match_threshold — this hypothesis didn't hold up against the full data.

    Returns (issue, verified_pattern) when the hypothesis is mechanically
    confirmed: issue is a formatted issue string, in exactly the shape
    check_rubric()'s other checks use and tagged "(discovered)" so it's visibly
    distinguishable in logs/reports; verified_pattern is
    {"pattern": str, "match_threshold": float} — the REAL regex and confirmed
    match fraction, threaded through so a later scoped fix (e.g. a composite-
    field column split) can use the pattern that was actually verified rather
    than re-deriving one from scratch.
    """
    series = df[column]
    non_null = series.dropna().astype(str)
    if len(non_null) < _EXPLORE_MIN_COLUMN_ROWS:
        return None, None

    if llm is None:
        from utils.llm_pick import pick_llm
        llm = pick_llm("cheap")

    human_content = (
        f"Column name: {column}\n"
        f"Real dtype: {series.dtype}\n\n"
        f"Hypothesis to verify:\n{hypothesis}"
    )
    try:
        structured_llm = llm.with_structured_output(VerifiedPatternProposal)
        proposal: VerifiedPatternProposal = structured_llm.invoke(
            [
                ("system", VERIFY_HYPOTHESIS_SYSTEM_PROMPT),
                ("human", human_content),
            ]
        )
    except Exception:
        return None, None

    try:
        compiled_pattern = re.compile(proposal.pattern)
    except re.error:
        return None, None

    matches = non_null.map(lambda v: bool(compiled_pattern.search(v)))
    match_count = int(matches.sum())
    match_frac = match_count / len(non_null)

    if match_frac < proposal.match_threshold:
        return None, None

    issue = (
        f"Composite field (discovered): column '{column}' has {match_count} value(s) "
        f"({match_frac:.0%}) matching the pattern '{proposal.pattern}' — this looks like two "
        f"distinct values glued together, not caught by a fixed rubric check."
    )
    # match_threshold here is the REAL, achieved match fraction (match_frac) — more
    # useful to a later fix-generation prompt than the LLM's originally proposed
    # minimum bar, which was just a threshold to clear, not the actual observed rate.
    verified_pattern = {"issue": issue, "pattern": proposal.pattern, "match_threshold": match_frac}
    return issue, verified_pattern


_COLUMN_NAME_TOKEN_RE = re.compile(r"[a-z]+")


def _column_name_token_overlap(col_a: str, col_b: str) -> float:
    """Jaccard overlap of lowercased word tokens between two column names —
    e.g. "Industry" / "Sector" share no tokens (0.0), but "customer_id" /
    "customer id" share everything (1.0). A cheap, dataset-agnostic heuristic
    for ranking which column pairs are worth asking the LLM about."""
    tokens_a = set(_COLUMN_NAME_TOKEN_RE.findall(col_a.lower()))
    tokens_b = set(_COLUMN_NAME_TOKEN_RE.findall(col_b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _quick_value_overlap(df: pd.DataFrame, col_a: str, col_b: str, sample_n: int = 20) -> float:
    """Cheap, approximate overlap fraction between two columns' real values,
    computed from a small sample — used only to RANK candidate pairs before
    deciding which ones are worth an LLM call. The actual accept/reject decision
    always re-checks the FULL columns (see _verify_column_pair)."""
    a = df[col_a].dropna().astype(str)
    b = df[col_b].dropna().astype(str)
    if a.empty or b.empty:
        return 0.0
    n = min(sample_n, len(a))
    a_sample = a.sample(n=n) if len(a) > n else a
    b_values = set(b.sample(n=min(sample_n * 5, len(b))))
    overlap = sum(1 for v in a_sample if v in b_values)
    return overlap / n if n else 0.0


def _rank_column_pairs(df: pd.DataFrame, columns: list) -> list:
    """Every column pair with any real signal (shared name tokens, or sampled
    value overlap), ranked highest-signal first. Pairs with zero signal on both
    measures are excluded entirely — nothing worth asking the LLM about."""
    scored = []
    for i, col_a in enumerate(columns):
        for col_b in columns[i + 1:]:
            name_score = _column_name_token_overlap(col_a, col_b)
            value_score = _quick_value_overlap(df, col_a, col_b)
            score = max(name_score, value_score)
            if score > 0:
                scored.append((score, col_a, col_b))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [(a, b) for _, a, b in scored]


def _verify_column_pair(df: pd.DataFrame, col_a: str, col_b: str) -> "str | None":
    """The trust boundary for column-pair discovery: actually compares the two
    FULL real columns (never just the sample) and only accepts the LLM's
    suspicion when the real overlap fraction clears _COLUMN_PAIR_OVERLAP_THRESHOLD.
    """
    both = df[[col_a, col_b]].dropna()
    if both.empty:
        return None
    equal_mask = both[col_a].astype(str) == both[col_b].astype(str)
    overlap_frac = float(equal_mask.mean())

    if overlap_frac < _COLUMN_PAIR_OVERLAP_THRESHOLD:
        return None

    return (
        f"Duplicate column (discovered): columns '{col_a}' and '{col_b}' are "
        f"{overlap_frac:.0%} identical — one may be silently derived from or "
        f"overwritten by the other, unintentionally destroying the original data "
        f"in one of them."
    )


EXPLORE_COLUMN_PAIR_SYSTEM_PROMPT = """You are given real sampled values from two columns
of the same dataset. Do not fix anything.

Do these two columns look suspiciously identical, or like one was silently derived from
or overwritten by the other in a way that looks unintentional (as opposed to two
columns that are legitimately, deliberately related — e.g. a subtotal and a total)? If
nothing looks suspicious, say so plainly — do not invent a finding to have something to
report.

State any finding as a testable hypothesis about the FULL columns, not just the sample."""


def _explore_column_pairs(df: pd.DataFrame, llm=None, max_pairs: "int | None" = None) -> list:
    """Cross-column consistency pass — catches the class of bug where one column
    is silently derived from/overwritten by another (e.g. Sector overwritten by
    a transform of Industry), which is invisible to any single-column check.

    Ranks candidate pairs (see _rank_column_pairs), asks the LLM about the
    top ones (capped at max_pairs, default EXPLORE_MAX_COLUMN_PAIRS), and only
    accepts a finding after mechanically re-comparing the two FULL real columns
    (see _verify_column_pair) — the LLM's suspicion alone is never enough.
    """
    columns = list(df.columns)
    if len(columns) < 2:
        return []
    limit = EXPLORE_MAX_COLUMN_PAIRS if max_pairs is None else max(0, min(max_pairs, EXPLORE_MAX_COLUMN_PAIRS))
    if limit == 0:
        return []

    if llm is None:
        from utils.llm_pick import pick_llm
        llm = pick_llm("cheap")

    candidate_pairs = _rank_column_pairs(df, columns)[:limit]

    issues = []
    for col_a, col_b in candidate_pairs:
        sample_a = _sample_column_values(df[col_a].dropna())[:10]
        sample_b = _sample_column_values(df[col_b].dropna())[:10]
        human_content = (
            f"Column A: {col_a}\nSample values: {sample_a}\n\n"
            f"Column B: {col_b}\nSample values: {sample_b}"
        )
        try:
            structured_llm = llm.with_structured_output(ExplorationHypothesis)
            result: ExplorationHypothesis = structured_llm.invoke(
                [
                    ("system", EXPLORE_COLUMN_PAIR_SYSTEM_PROMPT),
                    ("human", human_content),
                ]
            )
        except Exception:
            continue

        if not result.hypotheses:
            continue

        issue = _verify_column_pair(df, col_a, col_b)
        if issue:
            issues.append(issue)

    return issues


def explore_and_verify(
    df: "pd.DataFrame | None", llm=None, flagged_columns: "set | None" = None
) -> tuple:
    """Orchestrator: runs explore_column across every eligible column, verifies
    every returned hypothesis via _verify_hypothesis, runs _explore_column_pairs,
    and returns (issues, pattern_lookup):
    - issues: a flat list of issue strings in the exact format check_rubric()
      produces (so they merge into the same fail/warn pipeline with no new code
      path).
    - pattern_lookup: dict mapping each composite-field issue string to its
      verified pattern record ({"issue", "pattern", "match_threshold"} — see
      _verify_hypothesis) — threaded through so a later scoped fix (splitting a
      composite column) can use the pattern that was actually verified, rather
      than re-deriving one from scratch. Only composite-field issues appear
      here; duplicate-column issues (from _explore_column_pairs) have no regex
      pattern to thread through and are absent from this dict.

    flagged_columns: column names check_rubric already flagged with a fail-level
    issue this run — explore_column is skipped for these (no benefit to
    discovering more on top of a column already known to need fixing). Optional
    so this function stays usable standalone with just (df, llm).

    Cost/safety: tracks total LLM calls made (per-column exploration + per-
    hypothesis verification + column-pair exploration, combined) against
    EXPLORE_MAX_LLM_CALLS_PER_TABLE. If the ceiling is hit before everything is
    explored, logs a warning via print(..., file=sys.stderr) (same pattern as
    existing warnings in this file) and returns whatever was verified so far —
    never raises, never silently truncates without logging.
    """
    if df is None or df.empty:
        return [], {}

    if llm is None:
        from utils.llm_pick import pick_llm
        llm = pick_llm("cheap")

    flagged_columns = flagged_columns or set()
    issues: list = []
    pattern_lookup: dict = {}
    call_count = 0

    eligible_columns = [
        col for col in df.columns
        if col not in flagged_columns
        and not _is_long_form_prose_column(df[col])
        and not _is_sequential_id_like_column(df[col])
    ]

    for col in eligible_columns:
        if call_count >= EXPLORE_MAX_LLM_CALLS_PER_TABLE:
            print(
                f"[explore] LLM call ceiling ({EXPLORE_MAX_LLM_CALLS_PER_TABLE}) reached "
                f"before exploring column '{col}' — stopping discovery early for this table.",
                file=sys.stderr,
            )
            return issues, pattern_lookup

        hypotheses = explore_column(df, col, llm=llm)
        call_count += 1

        for hypothesis in hypotheses:
            if call_count >= EXPLORE_MAX_LLM_CALLS_PER_TABLE:
                print(
                    f"[explore] LLM call ceiling ({EXPLORE_MAX_LLM_CALLS_PER_TABLE}) reached "
                    f"while verifying hypotheses for column '{col}' — some hypotheses left "
                    "unverified.",
                    file=sys.stderr,
                )
                return issues, pattern_lookup
            verified_issue, verified_pattern = _verify_hypothesis(df, col, hypothesis, llm=llm)
            call_count += 1
            if verified_issue:
                issues.append(verified_issue)
                pattern_lookup[verified_issue] = verified_pattern

    remaining_budget = EXPLORE_MAX_LLM_CALLS_PER_TABLE - call_count
    if remaining_budget <= 0:
        print(
            f"[explore] LLM call ceiling ({EXPLORE_MAX_LLM_CALLS_PER_TABLE}) reached — "
            "skipping column-pair exploration for this table.",
            file=sys.stderr,
        )
        return issues, pattern_lookup

    pair_limit = min(EXPLORE_MAX_COLUMN_PAIRS, remaining_budget)
    issues.extend(_explore_column_pairs(df, llm=llm, max_pairs=pair_limit))
    return issues, pattern_lookup


# ---------------------------------------------------------------------------
# Piece 2: LLM-generated cleaning code, human approval gate, execution with
# retry-on-real-error, and the clean_dataset() orchestrator that ties it all
# together. This is the part clean_data.py, the enhanced load_data.py, and the
# ETL analyst's transform_load tool all call into — one implementation.
# ---------------------------------------------------------------------------

MAX_CLEAN_ATTEMPTS = 3

# Row-count-loss threshold: if a cleaned file has lost this fraction (or more) of its
# original row count, that's flagged explicitly in the result even when check_rubric now
# passes — a technically "clean" result that deleted a large share of the data is not
# automatically a good outcome, and should never be silently hidden inside a plain
# success message.
ROW_LOSS_FLAG_THRESHOLD = 0.20

# Issues in this set are purely about REPARSING/reshaping existing rows correctly
# (fixing which bytes belong to which field/row) — a genuinely correct fix for one of
# these can never change how many logical data rows the file has. This is distinct
# from issues like "Duplicate rows:" or "Dangling references:" where removing rows IS
# the correct, expected outcome. Without this distinction, a fix that accidentally
# splits one row into several (e.g. by mishandling an embedded newline while chasing
# down an unmatched quote) can pass _clean_issue_group's own textual re-check — which
# only asks "is the originally-flagged condition still detectable?" — while silently
# corrupting the file's row structure. See the incident this was added for: a
# "Column misalignment" fix on data/data-science-jobs/Uncleaned_DS_jobs.csv was
# accepted as "resolved" three times in a row while actually growing 672 rows into
# 778 malformed ones (job-description text split across bogus extra rows), which then
# failed at load time with a Postgres type error instead of being caught here.
ROW_COUNT_INTEGRITY_PREFIXES = (
    "Column misalignment:",
    "Structural issue:",
)


@dataclass
class IssueCleaningRecord:
    """One fail-level issue's individually-processed outcome within a file (see
    clean_dataset()'s per-issue loop). Fail-level issues are never batched together —
    each gets its own generate -> approve -> execute -> immediate re-check cycle,
    scoped to exactly this one issue.

    status: "resolved" | "skipped_declined" | "skipped_failed" (real execution error
    exhausted all attempts) | "skipped_incomplete" (execution succeeded but this
    specific issue was still detected immediately afterward, every attempt) |
    "declined_false_positive" (composite-field issues only: the fix-generation
    model itself determined discovery's pattern match was a false positive for
    this column and declined via the NO_SPLIT sentinel — a successful terminal
    outcome, distinct from "skipped_declined" (a HUMAN declined at the approval
    gate) since no human was ever asked; `error` holds the model's stated reason).
    """

    issue: str
    status: str = ""
    attempts: int = 0
    error: str = ""
    generated_code: str = ""


@dataclass
class WarnBatchRecord:
    """The batched outcome of a group of warn-level issues in a file, processed
    together in one combined generate -> approve -> execute -> re-check cycle.

    A file can now have MULTIPLE WarnBatchRecords (Spec 4): _group_issues_by_signature
    carves out its own batch for any set of 2+ warn issues sharing a real, mechanically-
    verified treatment signature (see _issue_treatment_signature); everything left over
    (signature None, or a signature unique among this file's warn issues) is pooled back
    into ONE final combined batch, preserving the original pre-Spec-4 "always one
    whole-file warn batch" behavior for anything that isn't a genuine signature match.

    status: "no_warn_issues" (nothing warn-level was flagged for this file — no clone
    mutation, no LLM call, no approval prompt happened for this stage) | "resolved" |
    "skipped_declined" | "skipped_failed" | "skipped_incomplete" (same meanings as
    IssueCleaningRecord.status above, just for the batch as a whole).
    """

    issues: list = field(default_factory=list)
    status: str = ""
    attempts: int = 0
    error: str = ""
    remaining_issues: list = field(default_factory=list)
    generated_code: str = ""


@dataclass
class FailBatchRecord:
    """The batched outcome of 2+ fail-level issues sharing an identical treatment
    signature (Spec 4 — see _issue_treatment_signature / _group_issues_by_signature),
    processed together in one combined generate -> approve -> execute -> re-check
    cycle — the same _clean_issue_group already used for a single fail-level issue
    or the warn-level batch, just called with more than one issue this time.

    signature: the treatment signature (e.g. ("placeholder", frozenset({'-1'})))
    that made these issues eligible to group — stored for transparency in logs/
    reports, not just internal bookkeeping (see _signature_to_jsonable for how this
    is serialized into cleaning_log.jsonl, since a signature may contain a frozenset).

    status/attempts/error/remaining_issues/generated_code: same meanings and same
    status vocabulary as IssueCleaningRecord and WarnBatchRecord
    ("resolved" | "skipped_declined" | "skipped_failed" | "skipped_incomplete").
    """

    issues: list = field(default_factory=list)
    signature: "tuple | None" = None
    status: str = ""
    attempts: int = 0
    error: str = ""
    remaining_issues: list = field(default_factory=list)
    generated_code: str = ""


def _signature_to_jsonable(signature: "tuple | None"):
    """Convert a treatment signature (which may contain a frozenset — not
    JSON-serializable on its own) into a plain, JSON-serializable form for the
    cleaning log, e.g. ("placeholder", frozenset({'-1'})) -> ["placeholder", ["-1"]].

    A dtype-aware signature (see _issue_treatment_signature) carries a third
    element (the value-type category string, e.g. "numeric") — passed through
    as-is since it's already JSON-serializable.
    """
    if signature is None:
        return None
    kind, payload, *rest = signature
    payload_jsonable = sorted(payload) if isinstance(payload, frozenset) else payload
    return [kind, payload_jsonable, *rest]


# Spec 7: Cleaning Recipe Cache. Generated cleaning code hardcodes the exact
# absolute cloned_path it was written against (the code-gen prompt tells the
# model "read and overwrite this exact path", and it does). A cached fix can't
# be replayed byte-for-byte on a LATER run — the path will be wrong — so the
# real path is swapped for this fixed placeholder before a recipe is stored,
# and swapped back for the current run's real path immediately before replay.
# A mechanical string substitution, never a semantic rewrite of the fix itself.
_RECIPE_PATH_PLACEHOLDER = "__CLEANING_RECIPE_FILE_PATH__"


def compute_recipe_id(table_name: str, signature: tuple) -> str:
    """Deterministic, stable across process restarts: sha256 over
    (table_name, signature) — same reasoning as
    utils.transformation_options.compute_candidate_id for why sha256 and not
    Python's built-in hash() (salted per-process for strings, so it would
    break the exact "same id across sessions" property this cache depends on).

    Scoped by table_name, never by signature alone — the same identical
    signature on a DIFFERENT table must never reuse this table's recipe (the
    same cross-table mixup class of bug _issue_treatment_signature's own
    dtype-aware check already exists to prevent elsewhere)."""
    payload = json.dumps(
        {"table": table_name, "signature": _signature_to_jsonable(signature)},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class FileCleaningRecord:
    """One file's outcome from clean_dataset(): either cleaned, or skipped.

    issues: every issue check_rubric() originally found for this file (fail-level +
    warn-level together, in the order check_rubric() produced them) — the same full
    list previous versions of this dataclass stored here, kept for anyone (tests,
    callers) that only cares about "what was wrong with this file", not how each
    issue was individually processed.

    fail_issue_records: list[IssueCleaningRecord], one per SINGLE (ungrouped)
    fail-level issue, in the order they were processed (== the order check_rubric()
    found them) — empty if this file had no ungrouped fail-level issues.

    fail_batch_records: list[FailBatchRecord] (Spec 4), one per GROUP of 2+
    fail-level issues sharing an identical treatment signature — empty if this
    file had no such groups. An issue that got grouped appears in exactly one
    FailBatchRecord.issues and never ALSO as a standalone IssueCleaningRecord —
    every fail-level issue lives in exactly one of fail_issue_records /
    fail_batch_records, never both, so attempts/summaries are never double-counted.

    warn_batches: list[WarnBatchRecord] (Spec 4 — plural; was a single optional
    WarnBatchRecord before this spec). Empty list if the file's processing was
    declined before ever reaching the warn-level stage (mid-way through the
    fail-level loop) — see WarnBatchRecord's own docstring for why there can now
    be more than one.

    status / attempts / error: an aggregate/overall view for this file — status is
    "cleaned" (final full re-check passed cleanly), "skipped_declined" (the user
    declined an approval prompt for some issue/batch — processing of the REST of the
    file stops at that point, same as the original whole-file design's decline
    semantics), or "skipped_incomplete" (every issue/batch got its full, individually-
    scoped chance, but the final full check_rubric() pass still found at least one of
    the file's original issues present). attempts is the sum of every fail-issue's,
    fail-batch's, and warn-batch's individual attempt counts, for a quick "how much
    retrying did this file need in total" figure.

    Post-cleaning-validation fields (final, whole-file check — a report/audit of the
    complete result after every issue already got its own appropriately-scoped retry
    loop above, not a further retry cycle itself):
    - rubric_recheck_passed: whether the FINAL, full check_rubric() pass across the
      complete cleaned file found none of the file's ORIGINAL issues still present.
    - remaining_issues: which of the file's original issues are still detectable after
      everything above has run — empty when rubric_recheck_passed is True.
    - row_count_before / row_count_after: real row counts of the file before any
      fail/warn processing started and after everything finished, for the row-loss
      check below. None when not computed (e.g. declined before completion).
    - row_loss_flagged: True if row_count_after lost >= ROW_LOSS_FLAG_THRESHOLD of
      row_count_before, even though rubric_recheck_passed is True — flagged, not
      treated as a failure, since the LLM's row-dropping strategy may be legitimate.

    structured_decomposition_candidates (Spec 3): purely informational — see
    _detect_range_columns. Populated from a scan of the file's REAL state after all
    fail/warn cleaning above has finished (regardless of outcome — even a
    "skipped_declined"/"skipped_incomplete" file still gets scanned), so currency/
    unit stripping has already had its chance to run before this looks for a
    numeric-range shape. Detecting a candidate here is NEVER itself a cleaning
    action and NEVER triggers a fix automatically — this is a schema-ENRICHMENT
    option (e.g. "$137K-$171K" -> min_salary/max_salary), not a data-quality
    problem (the original value here is not wrong), so it is deliberately kept out
    of `issues`/`FAIL_LEVEL_PREFIXES`/`WARN_LEVEL_PREFIXES` entirely. Something else
    (a CLI flag, a future orchestrator) decides whether to actually call
    decompose_range_column for a listed candidate — clean_dataset() itself never
    calls it.
    """

    file_name: str
    issues: list = field(default_factory=list)
    fail_issue_records: list = field(default_factory=list)
    fail_batch_records: list = field(default_factory=list)
    warn_batches: list = field(default_factory=list)
    status: str = ""  # "cleaned" | "skipped_declined" | "skipped_incomplete"
    attempts: int = 0
    error: str = ""
    rubric_recheck_passed: bool = True
    remaining_issues: list = field(default_factory=list)
    row_count_before: int | None = None
    row_count_after: int | None = None
    row_loss_flagged: bool = False
    structured_decomposition_candidates: list = field(default_factory=list)


def unresolved_issues_for_record(rec: "FileCleaningRecord") -> list:
    """Best-known list of a file's ORIGINAL issues that are still actually present,
    for use by any caller (currently utils/load_data.py) that needs to know a file's
    real post-cleaning data-quality state, not just its top-level status label.

    For "cleaned" / "skipped_incomplete" files this is simply rec.remaining_issues —
    clean_dataset() already computed it from a real, final check_rubric() re-check
    across the whole file, which is authoritative (not a bookkeeping guess).

    For "skipped_declined" files, that final re-check never ran (processing stopped
    the moment the user declined), so rec.remaining_issues is just its unused default
    ([]). This falls back to a bookkeeping computation instead: every original issue
    EXCEPT ones individually confirmed "resolved" before the decline point counts as
    still unresolved — a fail-level issue with its own IssueCleaningRecord.status ==
    "resolved", every issue in a FailBatchRecord whose status == "resolved" (Spec 4:
    a resolved batch means ALL of its member issues were fixed together), or every
    issue in a WarnBatchRecord whose status == "resolved" (now checked across
    potentially several warn_batches, not just one).
    """
    if rec.status != "skipped_declined":
        return rec.remaining_issues

    resolved_issues: set = {r.issue for r in rec.fail_issue_records if r.status == "resolved"}
    for batch in rec.fail_batch_records:
        if batch.status == "resolved":
            resolved_issues.update(batch.issues)
    for batch in rec.warn_batches:
        if batch.status == "resolved":
            resolved_issues.update(batch.issues)

    return [issue for issue in rec.issues if issue not in resolved_issues]


def _issue_outcome_line(status: str, attempts: int, error: str) -> str:
    """One human-readable outcome phrase for an IssueCleaningRecord/WarnBatchRecord
    status — shared by both so the wording is identical whether it's describing a
    single fail-level issue or the warn-level batch."""
    if status == "resolved":
        return f"resolved (attempts: {attempts})"
    if status == "skipped_declined":
        return "declined by user"
    if status == "skipped_failed":
        return f"execution failed every attempt (attempts: {attempts}): {error}"
    if status == "skipped_incomplete":
        return f"still present after {attempts} attempt(s): {error}"
    if status == "no_warn_issues":
        return "no warn-level issues found — nothing to do"
    return status or "(no outcome recorded)"


@dataclass
class CleaningResult:
    """Summary of a whole clean_dataset() run across every file in a folder."""

    folder_path: str
    cleaned_dir: str
    untouched_files: list = field(default_factory=list)  # filenames needing no cleaning
    cleaned_files: list = field(default_factory=list)  # list[FileCleaningRecord], status="cleaned"
    skipped_files: list = field(default_factory=list)  # list[FileCleaningRecord], skipped

    def _file_detail_lines(self, rec: "FileCleaningRecord") -> list:
        """Shared detail rendering for one file's fail-issue-by-fail-issue outcomes,
        any fail-level batches (Spec 4), the warn-level batch(es), and the final
        overall check — used for both cleaned and skipped files so the same real
        information is visible either way."""
        lines = []
        if rec.fail_issue_records:
            lines.append("        Fail-level issues (processed individually):")
            for issue_rec in rec.fail_issue_records:
                outcome = _issue_outcome_line(issue_rec.status, issue_rec.attempts, issue_rec.error)
                lines.append(f"          * {issue_rec.issue}")
                lines.append(f"              -> {outcome}")
        for batch_rec in rec.fail_batch_records:
            lines.append(
                f"        Fail-level issues (batched together, {len(batch_rec.issues)} identical, "
                f"signature={_signature_to_jsonable(batch_rec.signature)}):"
            )
            for issue in batch_rec.issues:
                lines.append(f"          * {issue}")
            outcome = _issue_outcome_line(batch_rec.status, batch_rec.attempts, batch_rec.error)
            lines.append(f"              -> batch {outcome}")
        for warn_batch in rec.warn_batches:
            if warn_batch.status == "no_warn_issues":
                continue
            lines.append(f"        Warn-level issues (batched together, {len(warn_batch.issues)}):")
            for issue in warn_batch.issues:
                lines.append(f"          * {issue}")
            outcome = _issue_outcome_line(warn_batch.status, warn_batch.attempts, warn_batch.error)
            lines.append(f"              -> batch {outcome}")
        if rec.status != "skipped_declined":
            if rec.rubric_recheck_passed:
                lines.append(
                    "        Final overall check: PASSED "
                    "(all originally-flagged issues confirmed resolved)"
                )
            else:
                remaining = "; ".join(rec.remaining_issues) or "(issue no longer describable)"
                lines.append(f"        Final overall check: cleaning ran but did not fully resolve: {remaining}")
            if rec.row_count_before is not None and rec.row_count_after is not None:
                before, after = rec.row_count_before, rec.row_count_after
                pct = (before - after) / before if before else 0.0
                lines.append(f"        Row count: {before} -> {after} ({pct:+.1%} change)")
                if rec.row_loss_flagged:
                    lines.append(
                        f"        \u26a0 WARNING: lost {pct:.1%} of rows during cleaning "
                        f"(>= {ROW_LOSS_FLAG_THRESHOLD:.0%} threshold) — a technically "
                        "clean result that deleted a large share of the data; verify "
                        "this wasn't over-aggressive."
                    )
        return lines

    def summary(self) -> str:
        lines = [f"Cleaning summary for {self.folder_path}:"]
        lines.append(
            f"  Untouched (no issues found): {len(self.untouched_files)} "
            f"({', '.join(self.untouched_files) or 'none'})"
        )
        if self.cleaned_files:
            lines.append(f"  Cleaned successfully: {len(self.cleaned_files)}")
            for rec in self.cleaned_files:
                lines.append(f"    - {rec.file_name} (total attempts: {rec.attempts})")
                lines.extend(self._file_detail_lines(rec))
        else:
            lines.append("  Cleaned successfully: 0")
        if self.skipped_files:
            lines.append(f"  Skipped: {len(self.skipped_files)}")
            for rec in self.skipped_files:
                lines.append(f"    - {rec.file_name} ({rec.status})")
                lines.extend(self._file_detail_lines(rec))
        if self.cleaned_files:
            lines.append(f"  Cleaned output folder: {self.cleaned_dir}")
        return "\n".join(lines)


def _clone_file(file_path: Path, cleaned_dir: Path) -> Path:
    """Copy file_path into cleaned_dir (creating it if needed) and return the clone's
    path. The raw file at file_path is never opened for writing anywhere in this module.

    Bounded, two-generation versioning (architecture review point #20): if a clone
    from a previous cleaning run already exists at this path, it's rotated to
    <file>.previous BEFORE being overwritten with a fresh copy of the raw source —
    one generation of history, enough to inspect or recover the prior cleaned result
    if a new cleaning run produces something wrong, without keeping unbounded history.
    """
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    dest = cleaned_dir / file_path.name
    previous = cleaned_dir / f"{file_path.name}.previous"
    if dest.exists():
        shutil.copy2(dest, previous)
    shutil.copy2(file_path, dest)
    return dest


def _strip_code_formatting(text: str) -> str:
    """Strip markdown code fences around generated code, if the model added them anyway
    (same pattern as agents/sql_analyst.py's _strip_sql_formatting, kept local here since
    this module has no dependency on the sql analyst module and shouldn't gain one just
    for a five-line string helper)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


CLEANING_CODE_SYSTEM_PROMPT = """You are a data cleaning engineer. Given a description of \
specific, real data-quality issues found in one CSV file, write a short, self-contained \
Python script that fixes ONLY those issues, operating in place on the exact file path given.

Rules:
- Output ONLY raw Python code. No explanation, no commentary, no markdown fences, no backticks.
- The script must read the CSV at the exact path given, apply targeted fixes for the issues \
listed, and write the cleaned result back to that same path (overwrite it in place).
- Do not invent or drop columns, and do not change rows/values unrelated to the listed issues.
- Only address the specific issues given — do not "fix" anything not listed. This includes \
values you notice in the sample rows that merely LOOK unusual (an extreme-but-plausible price, \
a rare-but-real category) — an unusual value is not automatically wrong, and is not yours to \
alter unless it is explicitly named in the issues list below.
- Import any libraries you use (e.g. `import pandas as pd`) — nothing is pre-imported for you.
- The script will be executed exactly as returned, top to bottom, standalone.

Special reasoning required for two specific issue categories, when they appear below:

MISSING VALUES: each such issue states the real percentage of that column that's missing. Your \
chosen strategy must be proportional to that real number, not a reflexive default:
- Under ~20% missing: imputing is reasonable (mean/median for a numeric column; for a \
categorical column, use the mode when one category clearly dominates, or 'Unknown' when the \
distribution is roughly even across categories — per-column guidance is provided below).
- ~20% or more missing: do NOT silently impute — imputing most of a column risks inventing data \
that was never there. Prefer dropping the column, dropping the affected rows, or leaving the \
values null with a clear flag column, whichever fits the file best.
- Either way, add a one-line code comment immediately above the fix stating which strategy you \
chose, the real percentage this column is missing, and why that strategy fits that percentage. \
This comment is read by a human at an approval gate before the code ever runs — it must be \
honest and specific, not generic.

INVALID VALUES: this category is reserved for genuinely impossible values (a negative count/\
amount, a date outside any sane calendar range) — these are safe to correct as before. Do not \
extend this reasoning to values that are merely unusual but structurally possible (a very high \
but real-looking price, an uncommon-but-valid date) — those are potentially real signal, not \
errors, and altering them without being explicitly told to is fabrication, not cleaning. If \
you are genuinely unsure whether a value in this category is an error, leave it unchanged and \
say so explicitly in a code comment rather than guessing."""


def _describe_file_for_prompt(file_path: Path, df=None) -> str:
    """Build a real-sample-rows + real-column/dtype context block for one file, the same
    principle generate_sql's schema context uses: concrete data, not a generic instruction.
    Pass df when the caller has already loaded it to avoid reading the file twice."""
    if df is None:
        df = _read_csv_robust(file_path)
    col_info = "\n".join(f"  - {c}" for c in df.columns)
    sample = df.head(5)
    sample_lines = "\n".join(str(row.to_dict()) for _, row in sample.iterrows())
    return f"Columns:\n{col_info}\n\nSample rows (real, from this file):\n{sample_lines}"


# Missing-value percentage above which imputation is discouraged in the cleaning-code prompt
# (see CLEANING_CODE_SYSTEM_PROMPT) — same 20% figure named in the project spec, distinct from
# MISSING_VALUE_THRESHOLD (5%) which controls whether check_rubric flags the column at all.
MISSING_VALUE_IMPUTE_CEILING = 0.20

_MISSING_PCT_RE = re.compile(r"is (\d+(?:\.\d+)?)% missing")
_MISSING_COL_RE = re.compile(r"column '([^']+)'")
# Top-category value-count share at or above this → mode is a safe fill; below → "Unknown"
_CATEGORICAL_DOMINANT_THRESHOLD = 0.40


def _categorical_fill_advice(df: pd.DataFrame, col: str) -> str:
    """Return a fill-value recommendation for one categorical column.

    Returns a short sentence (no leading space) that is appended inside the per-column
    guidance note in the code-gen prompt.  Returns "" when the column is numeric or has
    too-high cardinality to be categorical, so the caller can skip it cleanly.

    Numeric exclusion uses _column_value_type_category rather than pandas' own dtype
    (Spec 6 bugfix): every column in this project is loaded with dtype=str (see
    _read_csv_robust), so `pd.api.types.is_numeric_dtype(series)` — this function's
    original check — could never actually be True. A genuinely numeric column could
    have been getting mode/'Unknown' categorical advice instead of being recognized
    as numeric at all. _column_value_type_category was already built for this exact
    dtype=str blind spot (see the dtype-aware issue-batching fix) and is reused here
    rather than inventing a second numeric detector."""
    if col not in df.columns:
        return ""
    series = df[col].dropna()
    if series.empty or _column_value_type_category(series) == "numeric":
        return ""
    n_unique = series.nunique()
    if n_unique == 0 or n_unique / len(series) > 0.5:
        return ""
    counts = series.value_counts(normalize=True)
    top_share = float(counts.iloc[0])
    top_value = counts.index[0]
    if top_share >= _CATEGORICAL_DOMINANT_THRESHOLD:
        return (
            f" One category dominates ('{top_value}' = {top_share:.0%} of non-null values) "
            f"— fill with the mode ('{top_value}')."
        )
    return (
        f" Distribution is roughly even (top category '{top_value}' is only {top_share:.0%}) "
        f"— use 'Unknown' instead of the mode to avoid manufacturing a false majority."
    )


def _numeric_fill_advice(df: pd.DataFrame, col: str) -> str:
    """Return a fill-value recommendation for one numeric column (Spec 6).

    Mirrors _categorical_fill_advice's exact shape/contract: a short sentence (no
    leading space) appended inside the per-column guidance note, or "" when the
    column isn't real/numeric enough for the caller to skip cleanly. Numeric
    detection is value-based (_column_value_type_category), not pandas' own dtype,
    for the same reason _categorical_fill_advice's exclusion check needed the same
    fix — every column here is loaded as dtype=str.

    Recommends the column's real median, never the mean: a mean fill silently
    absorbs skew (a handful of high outliers in a salary/revenue-shaped column
    would pull every imputed value toward them), while the median is robust to
    exactly that."""
    if col not in df.columns:
        return ""
    series = df[col].dropna()
    if series.empty or _column_value_type_category(series) != "numeric":
        return ""
    numeric_values = pd.to_numeric(series, errors="coerce").dropna()
    if numeric_values.empty:
        return ""
    median = numeric_values.median()
    return (
        f" This is a numeric column — fill with its real median ({median:g}), "
        f"not the mean, since a skewed distribution would pull a mean fill toward outliers."
    )


def _issue_guidance(issue: str, df=None) -> str:
    """Extra, deterministic guidance appended under one issue line in the code-gen prompt.

    Fires for "Missing values" issues: parses the real percentage from check_rubric's own
    issue string so the LLM gets a concrete number. When df is provided, also appends a
    fill-value recommendation based on the column's real value shape: mode vs 'Unknown'
    for a categorical column (_categorical_fill_advice), or the real median for a numeric
    one (_numeric_fill_advice, Spec 6) — never the mean, which would silently absorb skew."""
    if not issue.startswith("Missing values:"):
        return ""
    match = _MISSING_PCT_RE.search(issue)
    if not match:
        return ""
    pct = float(match.group(1))
    if pct < MISSING_VALUE_IMPUTE_CEILING * 100:
        note = (
            f"    (This column is {pct:.1f}% missing — under the "
            f"{MISSING_VALUE_IMPUTE_CEILING:.0%} ceiling, so imputing is reasonable here."
        )
        if df is not None:
            col_match = _MISSING_COL_RE.search(issue)
            if col_match:
                col = col_match.group(1)
                # Mutually exclusive by construction: each function returns ""
                # for the column shape the other one handles.
                advice = _categorical_fill_advice(df, col) or _numeric_fill_advice(df, col)
                if advice:
                    note += advice
        note += ")"
        return note
    return (
        f"    (This column is {pct:.1f}% missing — at or above the "
        f"{MISSING_VALUE_IMPUTE_CEILING:.0%} ceiling, so do NOT silently impute; prefer "
        "dropping the column/rows or flagging nulls instead.)"
    )


def _generate_cleaning_code(
    file_path: Path,
    issues: list,
    llm,
    previous_code: str = "",
    previous_error: str = "",
) -> str:
    """One LLM call producing a cleaning script targeting this file's specific issues.
    If previous_code/previous_error are given (a retry after a real execution failure),
    both are included so the model can see exactly what it tried and what broke."""
    df = _read_csv_robust(file_path)
    file_context = _describe_file_for_prompt(file_path, df=df)
    issue_lines_parts = []
    for issue in issues:
        issue_lines_parts.append(f"- {issue}")
        guidance = _issue_guidance(issue, df=df)
        if guidance:
            issue_lines_parts.append(guidance)
    issue_lines = "\n".join(issue_lines_parts)
    human_content = (
        f"File to clean (read and overwrite this exact path): {file_path}\n\n"
        f"{file_context}\n\n"
        f"Specific issues found in THIS file (fix only these):\n{issue_lines}"
    )

    # Optional per-dataset hints: plain, unstructured English written by the person who
    # knows this dataset — e.g. "a value of 0 in the quantity column means out-of-stock,
    # not missing" or "salary ranges should be split into real min/max columns". This is
    # domain knowledge, not a config format to learn. If data/<NAME>/hints.txt is absent,
    # behavior is completely unchanged — the file is never required.
    # The cloned file lives at <dataset>/cleaned/<file>, so hints.txt is at <dataset>/hints.txt.
    hints_path = file_path.parent.parent / "hints.txt"
    if hints_path.exists():
        hints_text = hints_path.read_text().strip()
        if hints_text:
            human_content += (
                f"\n\nDataset-specific context (domain knowledge about this data — "
                f"treat these facts as authoritative when generating fix code):\n{hints_text}"
            )

    if previous_error:
        human_content += (
            "\n\nA previous attempt at this file's cleaning script did not fully succeed — "
            f"fix the script so it actually works:\n{previous_error}"
            f"\n\nPrevious script was:\n{previous_code}"
        )

    response = llm.invoke(
        [
            ("system", CLEANING_CODE_SYSTEM_PROMPT),
            ("human", human_content),
        ]
    )
    content = response.content
    if isinstance(content, list):
        # Same shape some providers (e.g. claude-sonnet-5 with extended thinking) can
        # return for agents/sql_analyst.py's _extract_text — normalize the same way.
        text = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if not (isinstance(block, dict) and block.get("type") == "thinking")
        )
    else:
        text = content
    return _strip_code_formatting(text)


# ---------------------------------------------------------------------------
# Scoped fix path for "Composite field (discovered):" issues only.
#
# CLEANING_CODE_SYSTEM_PROMPT above explicitly forbids adding or dropping
# columns — correct for every other issue category. But the honest fix for a
# composite field genuinely requires producing two columns from one and
# removing the original. Rather than loosen the general-purpose rule
# everywhere, this is one narrow, explicitly-scoped exception that fires ONLY
# for this one issue category (see _clean_issue_group's branch below) — every
# other issue still goes through _generate_cleaning_code /
# CLEANING_CODE_SYSTEM_PROMPT completely unchanged.
# ---------------------------------------------------------------------------

COMPOSITE_FIELD_SPLIT_SYSTEM_PROMPT = """You are fixing exactly one specific data-quality
issue: a column that has been mechanically confirmed to match a pattern suggesting it may
hold two distinct values glued together (e.g. a name with a rating appended, or a
"city, state" pair in one field).

IMPORTANT — check this first: the pattern match was computed mechanically and can be a
false positive. Look at the real sample values given below. If this column does NOT
actually contain two distinct, meaningfully different pieces of information — for example,
it is a sequential ID, a single coherent value that just happens to match the pattern by
coincidence, or splitting it would produce two meaningless fragments — output exactly this
and nothing else:

# NO_SPLIT: <one-sentence reason this column should not be split>

Do not attempt to force a split just because the pattern matched. A correct "no split
needed" response is a successful outcome, not a failure — you will not be asked to retry
if you decline for a real reason.

If the column DOES genuinely hold two distinct values, proceed with the rules below:
- You may split the named column into EXACTLY TWO new columns, using the verified pattern
  to separate the two parts. Name the new columns descriptively based on what each part
  actually represents (e.g. "company_name" and "company_rating", or "city" and "state").
- You MUST drop the original composite column after the split — do not leave stale
  duplicate data behind.
- You MUST NOT touch, rename, reorder, or drop any other column in the file.
- You MUST NOT invent a third column, a summary column, or any derived value beyond the
  two parts the pattern actually captures.
- For any value that does NOT match the verified pattern, split what you can and leave the
  unmatched part as null/NaN — do not guess or fabricate a value that isn't actually there.
- Output ONLY raw Python code — no explanation, no markdown fences, no backticks — UNLESS
  you are declining, in which case output ONLY the "# NO_SPLIT: ..." line above.
"""

_NO_SPLIT_RE = re.compile(r"^#\s*NO_SPLIT:\s*(.+)$", re.IGNORECASE)


def _extract_no_split_reason(code: str) -> "str | None":
    """Return the decline reason if code is a NO_SPLIT sentinel response from
    _generate_composite_split_code, else None. Checked against the stripped,
    single-purpose output COMPOSITE_FIELD_SPLIT_SYSTEM_PROMPT instructs the model
    to produce when it decides a column shouldn't be split — not a general-purpose
    text search that could false-match a comment inside a real split script.
    """
    stripped = code.strip()
    match = _NO_SPLIT_RE.match(stripped)
    return match.group(1).strip() if match else None


def _generate_composite_split_code(
    file_path: Path,
    issue: str,
    verified_pattern: dict,
    llm,
    previous_code: str = "",
    previous_error: str = "",
) -> str:
    """Mirrors _generate_cleaning_code's shape and prompt-building approach, but
    scoped to exactly one composite-field split: uses
    COMPOSITE_FIELD_SPLIT_SYSTEM_PROMPT (the one narrow exception to "never add/
    drop columns") and states the ALREADY-VERIFIED regex + real match fraction
    explicitly, so the model uses the pattern that was actually confirmed against
    the real data rather than re-deriving one from scratch.

    verified_pattern: {"pattern": str, "match_threshold": float} (see
    _verify_hypothesis) — the real regex and the real fraction of the column's
    values it was confirmed to match.
    """
    df = _read_csv_robust(file_path)
    file_context = _describe_file_for_prompt(file_path, df=df)
    pattern = verified_pattern.get("pattern", "")
    match_threshold = verified_pattern.get("match_threshold", 0.0)

    human_content = (
        f"File to clean (read and overwrite this exact path): {file_path}\n\n"
        f"{file_context}\n\n"
        f"The specific composite-field issue to fix (fix only this):\n- {issue}\n\n"
        "The verified pattern already mechanically confirmed against the real column's "
        f"full data:\n"
        f"  Regex: {pattern!r}\n"
        f"  Confirmed to match {match_threshold:.0%} of the real non-null values.\n"
        "Use this exact pattern to separate the two parts — do not re-derive your own "
        "pattern from scratch."
    )

    # Same optional per-dataset hints as _generate_cleaning_code — see its comment
    # for why (domain knowledge, not a config format; absent file = no behavior change).
    hints_path = file_path.parent.parent / "hints.txt"
    if hints_path.exists():
        hints_text = hints_path.read_text().strip()
        if hints_text:
            human_content += (
                f"\n\nDataset-specific context (domain knowledge about this data — "
                f"treat these facts as authoritative when generating fix code):\n{hints_text}"
            )

    if previous_error:
        human_content += (
            "\n\nA previous attempt at this file's split script did not fully succeed — "
            f"fix the script so it actually works:\n{previous_error}"
            f"\n\nPrevious script was:\n{previous_code}"
        )

    response = llm.invoke(
        [
            ("system", COMPOSITE_FIELD_SPLIT_SYSTEM_PROMPT),
            ("human", human_content),
        ]
    )
    content = response.content
    if isinstance(content, list):
        text = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if not (isinstance(block, dict) and block.get("type") == "thinking")
        )
    else:
        text = content
    return _strip_code_formatting(text)


def _request_approval(code: str, file_path: Path) -> bool:
    """The approval gate. Prints the full generated code and blocks on a real input()
    call — not a log line, not a config flag, not Hermes's own tool-approval system
    (which cannot see inside a script's own exec() call). This is the one and only
    place a cleaning script gets a chance to run, for every caller, every time.

    No interactivity check here: this function is shared by every caller of
    clean_dataset(), including manual CLI usage (clean_data.py, utils/load_data.py)
    and this project's own test suite, both of which deliberately pipe 'yes\\n'
    answers to a non-tty stdin and rely on input() reading them successfully — see
    the data-agent-architecture skill's testing conventions. The non-interactive
    fail-closed gate (architecture review point #24) belongs specifically to the
    SQL analyst graph's auto-clean redirect (agents/sql_analyst.py:clean_and_reload),
    since that is the one path a plain, read-only question can trigger automatically
    with no explicit cleaning request from the user — see its docstring.
    """
    print("\n" + "=" * 70)
    print(f"GENERATED CLEANING CODE for: {file_path}")
    print("=" * 70)
    print(code)
    print("=" * 70)
    answer = input(f"Run this code against {file_path}? Type 'yes' to approve, anything else to decline: ")
    return answer.strip().lower() == "yes"


def _execute_cleaning_code(code: str, file_path: Path):
    """Execute approved code in a fresh global namespace. Returns (success, error_str).
    Only ever called against a clone path — never the original raw file — by the caller
    below, which is what makes 'raw files are never modified' actually true rather than
    just documented."""
    try:
        exec(compile(code, f"<cleaning_code:{file_path.name}>", "exec"), {"__name__": "__cleaning__"})
        return True, ""
    except Exception:
        return False, traceback.format_exc()


def _count_csv_rows(path: Path) -> int | None:
    """Real row count of a CSV, used for the before/after row-count-loss check. Returns
    None (rather than raising) if the file can't be read at all — a file so broken it
    can't even be counted shouldn't crash the whole cleaning run over a metric."""
    try:
        return len(_read_csv_robust(path))
    except Exception:
        return None


_COMPOSITE_FIELD_PREFIX = "Composite field (discovered):"


def _composite_split_shape_ok(original_columns: list, current_columns: list, flagged_column: str) -> tuple:
    """The real post-fix check for a composite-field split (see _clean_issue_group):
    check_rubric() can never re-detect a "Composite field (discovered): ..." issue
    string (it has no code path that produces that text at all), so its generic
    "is the original issue string still present" re-check trivially always passes
    for this issue type — it is not actually verifying anything for a composite
    split. This is the real verification instead: the only shape a genuine two-way
    split can honestly produce is the originally-flagged column GONE and EXACTLY
    TWO brand-new columns (not present before) in its place.

    Returns (ok, reason). reason is "" when ok is True; otherwise it is the exact
    reason string the spec for this check requires, parameterized by how many
    replacement columns were actually found (0 if the column was simply dropped
    with no replacement, 3+ if extra columns appeared, anything but 2 is a
    violation — including implicitly when the flagged column was never actually
    dropped, since real callers always drop it as part of "df.to_csv(...)").
    """
    new_columns = [c for c in current_columns if c not in original_columns]
    ok = flagged_column not in current_columns and len(new_columns) == 2
    if ok:
        return True, ""
    return False, (
        f"Expected exactly 2 replacement columns after composite-field split, "
        f"found {len(new_columns)}."
    )


def _clean_issue_group(
    cloned_path: Path, target_issues: list, llm, pattern_lookup: "dict | None" = None,
    recipe_conn=None, table_name: "str | None" = None, signature: "tuple | None" = None,
) -> tuple:
    """Shared generate -> approve -> execute -> immediate re-check -> retry cycle for
    ONE group of issues against one already-cloned file, capped at MAX_CLEAN_ATTEMPTS
    for this group specifically. Used for both:
    - a single fail-level issue (target_issues == [that one issue]), called once per
      fail-level issue by clean_dataset()'s per-issue loop, and
    - the full batch of warn-level issues (target_issues == all warn-level issues for
      this file), called once for the whole batch — unchanged in spirit from the
      original whole-file design.

    The re-check after a successful execution only looks at whether THIS group's own
    target_issues are still present in a fresh check_rubric() run — never the full
    rubric — exactly matching "re-check ONLY this specific issue" for the single-issue
    case, and "re-check the batch" for the warn-level case.

    pattern_lookup: dict mapping a composite-field issue string to its verified
    pattern record (see explore_and_verify / _verify_hypothesis). Only consulted
    when target_issues is a single composite-field issue (always the case in
    practice — composite-field issues are fail-level, so clean_dataset() always
    processes them one at a time, never batched). When present, routes code
    generation to _generate_composite_split_code (the one narrow, explicitly-
    scoped exception permitted to add/drop columns) instead of the general-
    purpose _generate_cleaning_code, and adds an extra post-fix column-shape
    check that check_rubric()'s generic re-check cannot provide for this issue
    type (see _composite_split_shape_ok). Every other issue category is
    completely unaffected by this parameter.

    recipe_conn / table_name / signature (Spec 7, all optional, default None):
    when all three are given and signature is not None (one of the six defined
    treatment-signature categories — see _issue_treatment_signature; composite-
    field issues never have one, so this never interacts with the composite
    branch below), the FIRST attempt tries a previously-approved recipe for this
    exact (table_name, signature) before ever calling the LLM or _request_approval
    — zero new LLM calls, zero new approval prompts, but the SAME post-execution
    verification below still runs for real. If that replay doesn't actually
    resolve the issue (execution error, or the issue is still detected
    afterward), the cached attempt is discarded silently and the very next
    attempt falls through to a completely fresh, real generate/approve cycle —
    never treated as if the LLM itself had produced and failed that code. A
    freshly-generated fix that resolves a signature-eligible group (never a
    cache hit) is saved as the new recipe for next time.

    A composite-field fix-generation call can also legitimately decline (the
    NO_SPLIT sentinel, see _extract_no_split_reason) when it determines
    discovery's pattern match was a false positive for this column — that is a
    terminal, successful outcome returned immediately as "declined_false_positive",
    with NO retry, NO execution, and NO approval prompt (nothing was generated
    worth a human reviewing). This is deliberately NOT treated as a failure fed
    back into the retry loop: doing so was the original hole this exists to
    close — retry pressure ("expected 2 replacement columns, found 0 — try
    again") could otherwise push the model toward fabricating a shape-compliant
    but meaningless split just to satisfy _composite_split_shape_ok's letter.

    Returns (status, attempts, error, remaining_issues, last_code):
    - status: "resolved" | "skipped_declined" | "skipped_failed" | "skipped_incomplete"
      | "declined_false_positive" (composite-field issues only — see above)
    - attempts: how many attempts this group actually took
    - error: the real last error/still-present description (empty if resolved/declined);
      for "declined_false_positive", the model's own stated decline reason
    - remaining_issues: which of target_issues are still detected after the last
      attempt (empty unless status == "skipped_incomplete")
    - last_code: the last code that was approved and executed (empty if declined, or if
      execution never succeeded); used by clean_dataset() to capture reasoning comments.
    """
    previous_code = ""
    previous_error = ""
    remaining_issues = list(target_issues)
    attempt = 0
    last_executed_code = ""

    # Row-count-integrity baseline: only meaningful (and only checked) when at least
    # one of this group's target_issues is a reparsing/reshaping issue for which a
    # correct fix can never change the row count — see ROW_COUNT_INTEGRITY_PREFIXES.
    needs_row_count_integrity = any(
        issue.startswith(ROW_COUNT_INTEGRITY_PREFIXES) for issue in target_issues
    )
    row_count_before_group = _count_csv_rows(cloned_path) if needs_row_count_integrity else None

    # Composite-field split detection + baseline (see _composite_split_shape_ok):
    # only ever true for a singleton fail-level issue (composite-field issues are
    # never batched — see this function's own docstring above).
    is_composite_field_fix = (
        len(target_issues) == 1 and target_issues[0].startswith(_COMPOSITE_FIELD_PREFIX)
    )
    composite_original_columns = None
    composite_flagged_column = None
    if is_composite_field_fix:
        try:
            composite_original_columns = list(_read_csv_robust(cloned_path).columns)
        except Exception:
            composite_original_columns = None
        column_match = _ISSUE_COLUMN_RE.search(target_issues[0])
        composite_flagged_column = column_match.group(1) if column_match else None

    while attempt < MAX_CLEAN_ATTEMPTS:
        attempt += 1

        # Spec 7: try a previously-approved recipe exactly once, on the first
        # attempt only, before ever calling the LLM or the approval gate.
        # is_composite_field_fix and a real signature never co-occur (composite-
        # field issues aren't one of the six signature-defined categories), so
        # this and the composite branch below never both apply to the same group.
        used_cached_recipe = False
        if (
            attempt == 1
            and recipe_conn is not None
            and table_name is not None
            and signature is not None
        ):
            from utils.load_data import read_cleaning_recipe  # local import: see clean_dataset's pick_llm import for why

            recipe_id = compute_recipe_id(table_name, signature)
            cached = read_cleaning_recipe(recipe_conn, table_name, recipe_id)
            if cached is not None:
                code = cached["generated_code"].replace(_RECIPE_PATH_PLACEHOLDER, str(cloned_path))
                used_cached_recipe = True
                print(
                    f"[recipe] Reusing a previously-approved fix for '{table_name}' "
                    f"— no new AI call, no new approval needed: {cloned_path}",
                )

        if not used_cached_recipe:
            if is_composite_field_fix:
                issue = target_issues[0]
                verified_pattern = (pattern_lookup or {}).get(issue)
                if verified_pattern is not None:
                    code = _generate_composite_split_code(
                        cloned_path, issue, verified_pattern, llm, previous_code, previous_error
                    )
                    # Legitimate decline: the model itself determined discovery's
                    # pattern match was a false positive for this column. This is a
                    # terminal, successful outcome — NOT fed back into the retry
                    # loop as a failure (that pressure is exactly what could push a
                    # later attempt toward fabricating a shape-compliant-but-
                    # meaningless split just to satisfy _composite_split_shape_ok).
                    # No execution, no approval prompt: nothing was generated that's
                    # worth a human reviewing.
                    no_split_reason = _extract_no_split_reason(code)
                    if no_split_reason is not None:
                        return "declined_false_positive", attempt, no_split_reason, [], ""
                else:
                    print(
                        f"[explore] No verified pattern found for composite-field issue "
                        f"{issue!r} — falling back to the general-purpose cleaning generator.",
                        file=sys.stderr,
                    )
                    code = _generate_cleaning_code(cloned_path, remaining_issues, llm, previous_code, previous_error)
            else:
                code = _generate_cleaning_code(cloned_path, remaining_issues, llm, previous_code, previous_error)

        approved = True if used_cached_recipe else _request_approval(code, cloned_path)
        if not approved:
            return "skipped_declined", attempt, "", [], ""

        success, error = _execute_cleaning_code(code, cloned_path)
        if not success:
            if used_cached_recipe:
                # The cached fix doesn't even run against this file anymore —
                # discard it silently and let the next attempt generate a
                # completely fresh fix, rather than feeding this stale code back
                # into the LLM as if it had been THIS session's own failed try.
                print(
                    f"[recipe] cached recipe for '{table_name}' failed to execute — "
                    f"falling back to fresh generation",
                    file=sys.stderr,
                )
                continue
            previous_code, previous_error = code, error
            if attempt >= MAX_CLEAN_ATTEMPTS:
                return "skipped_failed", attempt, error, [], last_executed_code
            continue

        last_executed_code = code

        # Execution succeeded — but that alone is not proof this group's issue(s) are
        # actually gone. Re-run check_rubric() against the real result and check
        # whether any of THIS group's own target issues are still detectable (a fresh,
        # unrelated issue elsewhere in the file is out of scope for this group's retry
        # loop — it belongs to whichever other group, if any, is responsible for it).
        post_issues = check_rubric(cloned_path)
        still_present = [issue for issue in remaining_issues if issue in post_issues]

        # Row-count-integrity check: check_rubric() only re-tests the textual
        # condition it originally flagged (e.g. "is there still an unmatched quote
        # character") — it has no idea whether the fix also silently reshaped the
        # file's row structure. A misalignment/structural fix that mishandles an
        # embedded newline can make the flagged condition disappear (so still_present
        # comes back empty) while actually splitting or merging real rows. Treat that
        # as NOT resolved — a corrupted-but-textually-clean result is worse than an
        # honestly-still-flagged one, since it can silently pass through to the
        # database load step and fail there with a much less diagnosable error (see
        # ROW_COUNT_INTEGRITY_PREFIXES' docstring for the real incident this covers).
        row_count_changed = False
        row_count_after_attempt = None
        if not still_present and needs_row_count_integrity and row_count_before_group is not None:
            row_count_after_attempt = _count_csv_rows(cloned_path)
            if row_count_after_attempt is not None and row_count_after_attempt != row_count_before_group:
                row_count_changed = True

        # Composite-field shape check: check_rubric()'s re-check above can NEVER
        # re-detect a "Composite field (discovered): ..." string (see
        # _composite_split_shape_ok's docstring) — still_present is trivially
        # always [] for this issue type, so this is the check doing the REAL
        # verification work, not a redundant extra. Only evaluated once the
        # generic checks above would otherwise already call this "resolved".
        composite_shape_ok = True
        composite_shape_reason = ""
        if (
            not still_present
            and not row_count_changed
            and is_composite_field_fix
            and composite_original_columns is not None
            and composite_flagged_column is not None
        ):
            try:
                current_columns = list(_read_csv_robust(cloned_path).columns)
            except Exception:
                current_columns = None
            if current_columns is not None:
                composite_shape_ok, composite_shape_reason = _composite_split_shape_ok(
                    composite_original_columns, current_columns, composite_flagged_column
                )

        if not still_present and not row_count_changed and composite_shape_ok:
            # Spec 7: a freshly-generated (never a cache hit) fix that just got
            # approved and verified for a signature-eligible group is saved as
            # the recipe for next time — never re-saved on a cache hit itself,
            # since that would just be writing back the exact same thing.
            if (
                not used_cached_recipe
                and recipe_conn is not None
                and table_name is not None
                and signature is not None
            ):
                from utils.load_data import write_cleaning_recipe  # local import: see clean_dataset's pick_llm import for why

                placeholder_code = code.replace(str(cloned_path), _RECIPE_PATH_PLACEHOLDER)
                write_cleaning_recipe(
                    recipe_conn, table_name, compute_recipe_id(table_name, signature),
                    signature, placeholder_code,
                )
            return "resolved", attempt, "", [], last_executed_code

        if used_cached_recipe:
            # The cached fix ran without raising, but didn't actually resolve
            # the issue against this file's real current data (still detected,
            # or it corrupted the row count/shape). Discard it silently and
            # retry as a completely fresh generation next attempt — never treat
            # a stale recipe's own code as if it were this session's failed
            # attempt (previous_code/previous_error must stay whatever they
            # were before this replay, i.e. empty on this first attempt).
            print(
                f"[recipe] cached recipe for '{table_name}' did not resolve the "
                f"issue on this file's real current data — falling back to fresh generation",
                file=sys.stderr,
            )
            continue

        if row_count_changed:
            remaining_issues = list(target_issues)
            previous_code = code
            previous_error = (
                "The script executed without raising an exception, and the "
                "originally-flagged issue text is no longer detected, but the file's "
                f"row count changed from {row_count_before_group} to "
                f"{row_count_after_attempt} — a correct fix for this issue must "
                "preserve every existing row (only reshape/reparse them), so this is "
                "NOT actually resolved. Re-generate a fix that corrects the field/row "
                "boundaries without adding, splitting, or dropping any row."
            )
        elif not composite_shape_ok:
            remaining_issues = list(target_issues)
            previous_code = code
            previous_error = composite_shape_reason
        else:
            remaining_issues = still_present
            previous_code = code
            previous_error = (
                "The script executed without raising an exception, but re-running the "
                "data-quality check against the real cleaned output found these "
                "originally-listed issue(s) are STILL PRESENT (not fixed):\n"
                + "\n".join(f"- {i}" for i in still_present)
            )
        if attempt >= MAX_CLEAN_ATTEMPTS:
            return "skipped_incomplete", attempt, previous_error, remaining_issues, last_executed_code

    # Unreachable in practice (the while loop always returns before falling off the
    # end, since MAX_CLEAN_ATTEMPTS >= 1), but keeps this function's return type
    # honest rather than implicitly returning None if MAX_CLEAN_ATTEMPTS were ever 0.
    return "skipped_incomplete", attempt, previous_error, remaining_issues, last_executed_code


# ---------------------------------------------------------------------------
# Spec 3: numeric range decomposition (Salary/Revenue -> min/max/avg columns).
#
# Deliberately NOT the same kind of fix as composite-field splitting. A composite
# field is a data-quality PROBLEM (two variables wrongly glued together — the
# glued-together form is simply wrong). A salary/revenue range is not wrong —
# "$137K-$171K" is a perfectly valid, correctly-formatted piece of information;
# decomposing it into min_salary/max_salary/avg_salary is a deliberate SCHEMA
# ENRICHMENT decision, closer to a migration than a cleanup. It gets its own
# detection flag (never added to check_rubric's issue list or
# FAIL_LEVEL_PREFIXES/WARN_LEVEL_PREFIXES), its own prompt, and its own approval
# flow — entirely separate from check_rubric's issue list and from the
# composite-field split path above.
#
# _detect_range_columns is purely informational and runs automatically inside
# clean_dataset() (see below) — it never triggers a fix on its own.
# decompose_range_column is the opt-in fix itself, and is NEVER called
# automatically from clean_dataset(); something else (a CLI flag, a separate
# pipeline stage, a future orchestrator built for this) decides whether/when to
# actually call it for a listed candidate. This keeps "did the data get cleaned"
# and "did we choose to enrich the schema" as two clearly separable,
# independently-auditable decisions.
# ---------------------------------------------------------------------------

# Match-fraction threshold for a column to be listed as a range-decomposition
# candidate — mirrors RANGE_DECOMPOSITION_MATCH_THRESHOLD's composite-field
# counterpart (_verify_hypothesis's match_threshold concept) for consistency.
RANGE_DECOMPOSITION_MATCH_THRESHOLD = 0.8

# Scale-word suffixes commonly used INSIDE a numeric range value (e.g. "$137K",
# "$1 to $2 billion") — distinct from UNIT_SUFFIXES (physical units like kg/cm):
# these represent an implicit multiplier on the number itself, not a unit of
# measurement, so a bare unit-suffix check was never meant to cover them.
_RANGE_SCALE_SUFFIXES = ("thousand", "million", "billion", "k", "mm", "bn", "m", "b")
_RANGE_SCALE_ALT = "|".join(re.escape(s) for s in _RANGE_SCALE_SUFFIXES)

# One "side" of a range: an optional currency symbol, digits (optional commas/
# decimal), then an optional scale suffix — e.g. matches "$137K", "$1", "2",
# "10,000.50". Reuses CURRENCY_SYMBOLS (the same vocabulary check_rubric's own
# _check_currency_unit_symbols is built on) rather than inventing a new one.
_RANGE_NUMBER_PART = (
    rf"[{re.escape(CURRENCY_SYMBOLS)}]?\s*\d[\d,]*(?:\.\d+)?\s*(?:{_RANGE_SCALE_ALT})?"
)
# Two of those sides joined by "-" or "to" — anchored at the start only (real
# values commonly have trailing junk like " (Glassdoor est.)" / " (USD)" that
# this deliberately does not require matching).
_RANGE_PAIR_RE = re.compile(
    rf"^\s*{_RANGE_NUMBER_PART}\s*(?:-|to)\s*{_RANGE_NUMBER_PART}",
    re.IGNORECASE,
)

# Open-ended range shapes (Spec 1, Part 6) — a real range value with only ONE
# bound known, not a pair. Seen live in this dataset's own Revenue column
# ("$10+ billion (USD)", "Less than $1 million (USD)"), which a pair-only
# regex structurally cannot recognize as range-shaped at all — this is exactly
# the gap that caused Revenue's real match fraction to undercount before this
# fix (a "-" or "to" pair is not the only legitimate range shape).
# Unbounded minimum: a number immediately followed by "+", e.g. "$10+ billion".
_RANGE_OPEN_MIN_RE = re.compile(
    rf"^\s*[{re.escape(CURRENCY_SYMBOLS)}]?\s*\d[\d,]*(?:\.\d+)?\+\s*(?:{_RANGE_SCALE_ALT})?",
    re.IGNORECASE,
)
# Unbounded maximum: "less than" (or "under") followed by one number part,
# e.g. "Less than $1 million".
_RANGE_OPEN_MAX_RE = re.compile(
    rf"^\s*(?:less\s+than|under)\s+{_RANGE_NUMBER_PART}",
    re.IGNORECASE,
)


def _detect_range_columns(df: pd.DataFrame) -> list:
    """Deterministic, no-LLM detector for columns that consistently hold a
    numeric range — a closed pair (e.g. "$137K-$171K (Glassdoor est.)", "$1 to
    $2 billion (USD)") or an open-ended bound (e.g. "$10+ billion (USD)",
    "Less than $1 million (USD)" — Spec 1, Part 6: seen live in this dataset's
    own Revenue column, and structurally invisible to a pair-only pattern). A
    candidate for OPT-IN structured decomposition (see decompose_range_column
    below), never automatically triggered and never added to check_rubric's
    issue list (see this section's module comment for why: this is a
    schema-enrichment option, not a data-quality problem).

    A value counts as a match if it matches ANY of: _RANGE_PAIR_RE (a closed
    "X-Y"/"X to Y" pair), _RANGE_OPEN_MIN_RE (an unbounded minimum, "X+"), or
    _RANGE_OPEN_MAX_RE ("less than X" / "under X", an unbounded maximum). All
    three tolerate an optional leading currency symbol and scale suffix, so
    this correctly recognizes a range shape whether or not upstream currency/
    unit cleanup has already stripped the surrounding $/K symbols by the time
    this runs (clean_dataset() runs this AFTER a file's normal cleaning
    finishes — see its wiring below) — no separate "has this column's
    currency issue already been resolved" check is needed, since the patterns
    themselves already handle both forms.

    Returns a list of {"column": str, "match_fraction": float,
    "sample_pattern": str} dicts for every column clearing
    RANGE_DECOMPOSITION_MATCH_THRESHOLD, ordered as columns appear in df. Never
    modifies df — purely a scan, same discipline as check_rubric.
    """
    candidates = []
    for col in df.columns:
        series = df[col].dropna().astype(str).str.strip()
        if series.empty:
            continue
        matches = (
            series.str.match(_RANGE_PAIR_RE)
            | series.str.match(_RANGE_OPEN_MIN_RE)
            | series.str.match(_RANGE_OPEN_MAX_RE)
        )
        match_count = int(matches.sum())
        match_fraction = match_count / len(series)
        if match_fraction >= RANGE_DECOMPOSITION_MATCH_THRESHOLD:
            sample_pattern = series[matches].iloc[0]
            candidates.append({
                "column": col,
                "match_fraction": match_fraction,
                "sample_pattern": sample_pattern,
            })
    return candidates


RANGE_DECOMPOSITION_SYSTEM_PROMPT = """You are enriching a dataset by decomposing one
column that holds a numeric range (e.g. "$137K-$171K", "$1 to $2 billion (USD)") into
separate numeric columns. This is NOT a data-quality fix — the original column is not
wrong, you are adding structured columns alongside it.

IMPORTANT — check this first: the range pattern was matched mechanically and can be a
false positive. Look at the real sample values given below. If this column does NOT
actually hold a genuine numeric range worth decomposing — for example, most values are
a single number rather than a range, a non-numeric placeholder ("Unknown", "-1", "N/A"),
or free text that only coincidentally matched the pattern — output exactly this and
nothing else:

# NO_DECOMPOSITION: <one-sentence reason this column should not be decomposed>

Do not force a decomposition just because the pattern matched. A correct "no
decomposition needed" response is a successful outcome, not a failure — you will not be
asked to retry if you decline for a real reason.

If the column DOES genuinely hold a numeric range worth decomposing, proceed with the
rules below:
- You may add up to 3 new numeric columns derived from the named column: a minimum
  value, a maximum value, and (optionally) an average of the two. Name them clearly and
  consistently, e.g. "min_salary", "max_salary", "avg_salary" for a salary column, or
  the equivalent naming for whatever the column represents.
- The ORIGINAL column MUST be preserved completely unchanged — do not modify, rename, or
  drop it.
- You MUST NOT touch any other column in the file.
- All added columns must use ONE consistent unit (e.g. convert "billion" ranges to the
  same unit as "million" ranges before writing the numeric value) — state your chosen
  unit in a one-line code comment.
- Some real values are OPEN-ENDED, not a closed pair — e.g. "$10+ billion" (a known
  minimum, no known maximum) or "Less than $1 million" (a known maximum, no known
  minimum). For these: fill in the ONE bound that's actually known, leave the OTHER
  bound null/NaN (never guess it), and leave the average null/NaN too (an average
  needs both real bounds — do not average a known bound with a guessed one).
- For any value that doesn't match a clean numeric range (unparseable, a single value
  with no range, a non-numeric placeholder), leave the new columns as null/NaN for that
  row rather than guessing.
- Output ONLY raw Python code — no explanation, no markdown fences, no backticks —
  UNLESS you are declining, in which case output ONLY the "# NO_DECOMPOSITION: ..." line
  above.
- The script must read the CSV at the exact path given, add the new columns, and write
  the result back to that same path (overwrite in place).
"""

_NO_DECOMPOSITION_RE = re.compile(r"^#\s*NO_DECOMPOSITION:\s*(.+)$", re.IGNORECASE)


def _extract_no_decomposition_reason(code: str) -> "str | None":
    """Return the decline reason if code is a NO_DECOMPOSITION sentinel response
    from _generate_range_decomposition_code, else None — same discipline as
    _extract_no_split_reason for the composite-field spec's NO_SPLIT sentinel.
    """
    stripped = code.strip()
    match = _NO_DECOMPOSITION_RE.match(stripped)
    return match.group(1).strip() if match else None


def _generate_range_decomposition_code(
    file_path: Path,
    column: str,
    candidate: dict,
    llm,
    previous_code: str = "",
    previous_error: str = "",
) -> str:
    """One LLM call producing a range-decomposition script for ONE candidate
    column, or a NO_DECOMPOSITION decline. Mirrors _generate_cleaning_code /
    _generate_composite_split_code's prompt-building shape (_describe_file_for_
    prompt, optional hints.txt, previous_code/previous_error on retries,
    _strip_code_formatting) but uses RANGE_DECOMPOSITION_SYSTEM_PROMPT and
    states the mechanically-detected match fraction + a real sample value
    explicitly, so the model has concrete evidence to judge the candidate
    against rather than just the column name.
    """
    df = _read_csv_robust(file_path)
    file_context = _describe_file_for_prompt(file_path, df=df)
    match_fraction = candidate.get("match_fraction", 0.0)
    sample_pattern = candidate.get("sample_pattern", "")

    human_content = (
        f"File to enrich (read and overwrite this exact path): {file_path}\n\n"
        f"{file_context}\n\n"
        f"The candidate column for structured range decomposition: '{column}'\n"
        f"Mechanically detected: {match_fraction:.0%} of its real non-null values match "
        f"a numeric-range shape (e.g. {sample_pattern!r}).\n\n"
        "Inspect the real sample values above for this column before deciding whether "
        "to decompose it or decline."
    )

    # Same optional per-dataset hints as _generate_cleaning_code — see its comment
    # for why (domain knowledge, not a config format; absent file = no behavior change).
    hints_path = file_path.parent.parent / "hints.txt"
    if hints_path.exists():
        hints_text = hints_path.read_text().strip()
        if hints_text:
            human_content += (
                f"\n\nDataset-specific context (domain knowledge about this data — "
                f"treat these facts as authoritative when generating code):\n{hints_text}"
            )

    if previous_error:
        human_content += (
            "\n\nA previous attempt at this file's decomposition script did not fully "
            f"succeed — fix the script so it actually works:\n{previous_error}"
            f"\n\nPrevious script was:\n{previous_code}"
        )

    response = llm.invoke(
        [
            ("system", RANGE_DECOMPOSITION_SYSTEM_PROMPT),
            ("human", human_content),
        ]
    )
    content = response.content
    if isinstance(content, list):
        text = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if not (isinstance(block, dict) and block.get("type") == "thinking")
        )
    else:
        text = content
    return _strip_code_formatting(text)


def _range_decomposition_shape_ok(file_path: Path, original_columns: list, flagged_column: str) -> tuple:
    """Post-fix verification for a range decomposition — same discipline as
    _composite_split_shape_ok, but this is an ENRICHMENT, not a replacement: the
    original column must be PRESERVED (never dropped), and 1-3 new, genuinely
    numeric columns must exist that didn't exist before.

    Returns (ok, reason, new_columns). reason/new_columns are "" / [] when ok.
    """
    try:
        current_df = _read_csv_robust(file_path)
    except Exception:
        return False, "Could not read the file back after decomposition to verify its shape.", []

    current_columns = list(current_df.columns)
    if flagged_column not in current_columns:
        return False, (
            f"The original column '{flagged_column}' must be preserved unchanged, "
            "but it is no longer present after decomposition."
        ), []

    new_columns = [c for c in current_columns if c not in original_columns]
    if not (1 <= len(new_columns) <= 3):
        return False, (
            f"Expected 1-3 new numeric columns after range decomposition, "
            f"found {len(new_columns)}."
        ), []

    for new_col in new_columns:
        non_null = current_df[new_col].dropna()
        if non_null.empty:
            continue
        numeric = pd.to_numeric(non_null, errors="coerce")
        if numeric.isna().mean() > 0.5:
            return False, (
                f"New column '{new_col}' does not look numeric after decomposition."
            ), []

    return True, "", new_columns


def decompose_range_column(file_path: Path, candidate: dict, llm=None) -> dict:
    """Opt-in structured decomposition for ONE candidate column (see
    _detect_range_columns). Runs its own generate -> approve -> execute ->
    re-check cycle, entirely separate from clean_dataset()'s fail/warn issue
    loop. NEVER called automatically from clean_dataset() — see this section's
    module comment above for why (schema enrichment vs. data-quality fix).

    Reuses _request_approval (the exact same human approval gate every other
    fix in this pipeline uses — no new or weaker approval path for a schema-
    enrichment decision than for a real data-quality fix) and
    _execute_cleaning_code unchanged. Operates on file_path exactly as given —
    it does not clone or version anything itself; the caller decides which
    path (a raw file, or an already-cleaned clone) to pass in.

    Returns a result dict:
    {"column": str, "status": str, "attempts": int, "error": str,
     "new_columns": list, "generated_code": str}

    status: "resolved" | "skipped_declined" | "skipped_failed" |
    "skipped_incomplete" | "declined_false_positive" (the LLM itself determined
    this candidate isn't a genuine range column worth decomposing, via the
    NO_DECOMPOSITION sentinel — a terminal, successful, NON-retried outcome,
    exactly like the composite-field split's NO_SPLIT decline (Spec 2.1): the
    retry-pressure mistake from that spec (a shape-check failure fed back into
    the retry loop as an error, which could pressure the model into fabricating
    a shape-compliant-but-meaningless result) is deliberately not repeated here
    — the sentinel check happens immediately after generation, before any
    approval prompt or execution, on every attempt).
    """
    column = candidate["column"]
    if llm is None:
        from utils.llm_pick import pick_llm
        llm = pick_llm("high")

    try:
        original_columns = list(_read_csv_robust(file_path).columns)
    except Exception as e:
        return {
            "column": column, "status": "skipped_failed", "attempts": 0,
            "error": f"Could not read {file_path} to establish a baseline: {e}",
            "new_columns": [], "generated_code": "",
        }

    previous_code = ""
    previous_error = ""
    attempt = 0
    last_code = ""

    while attempt < MAX_CLEAN_ATTEMPTS:
        attempt += 1
        code = _generate_range_decomposition_code(
            file_path, column, candidate, llm, previous_code, previous_error
        )

        # Legitimate decline, checked BEFORE any approval prompt or execution —
        # a terminal, successful outcome, never fed back into the retry loop.
        no_decomposition_reason = _extract_no_decomposition_reason(code)
        if no_decomposition_reason is not None:
            return {
                "column": column, "status": "declined_false_positive", "attempts": attempt,
                "error": no_decomposition_reason, "new_columns": [], "generated_code": "",
            }

        approved = _request_approval(code, file_path)
        if not approved:
            return {
                "column": column, "status": "skipped_declined", "attempts": attempt,
                "error": "", "new_columns": [], "generated_code": "",
            }

        success, error = _execute_cleaning_code(code, file_path)
        if not success:
            previous_code, previous_error = code, error
            last_code = code
            if attempt >= MAX_CLEAN_ATTEMPTS:
                return {
                    "column": column, "status": "skipped_failed", "attempts": attempt,
                    "error": error, "new_columns": [], "generated_code": last_code,
                }
            continue

        last_code = code
        shape_ok, shape_reason, new_columns = _range_decomposition_shape_ok(
            file_path, original_columns, column
        )
        if shape_ok:
            return {
                "column": column, "status": "resolved", "attempts": attempt,
                "error": "", "new_columns": new_columns, "generated_code": last_code,
            }

        previous_code = code
        previous_error = shape_reason
        if attempt >= MAX_CLEAN_ATTEMPTS:
            return {
                "column": column, "status": "skipped_incomplete", "attempts": attempt,
                "error": shape_reason, "new_columns": [], "generated_code": last_code,
            }

    # Unreachable in practice (mirrors _clean_issue_group's own unreachable tail).
    return {
        "column": column, "status": "skipped_incomplete", "attempts": attempt,
        "error": previous_error, "new_columns": [], "generated_code": last_code,
    }


# ---------------------------------------------------------------------------
# Spec 1, Part 8: verbose categorical label simplification (e.g. "51 to 200
# employees" -> "51-200"). Purely a STYLE choice, not a correctness fix or an
# enrichment — nothing is objectively wrong with the verbose form, and no new
# column is added; the same column is rewritten in place with shorter,
# equivalent values. Kept out of check_rubric's issue list (same reasoning as
# range decomposition above: this is a judgment call, not a data-quality
# problem), detected purely mechanically (no LLM), and offered through
# Transformation Options only when relevant to the current question's
# category/grouping dimension (see utils.transformation_options.
# surface_relevant_transformations, which already does this generically via
# chart_category_column — no special-casing needed for this kind).
# ---------------------------------------------------------------------------

LABEL_SIMPLIFICATION_MATCH_THRESHOLD = 0.8

# A verbose "N [to M] <unit word>" categorical label — e.g. "51 to 200
# employees", "10000+ employees". Deliberately narrower than "any column
# whose values share a common trailing word" (which would over-fire on any
# descriptive categorical column) — this targets the specific redundant-
# numeric-range-plus-repeated-unit-word shape the reference notebook actually
# shortens.
_VERBOSE_LABEL_RE = re.compile(
    r"^\d[\d,]*\+?\s*(?:to\s+\d[\d,]*\+?)?\s+[A-Za-z]+$",
    re.IGNORECASE,
)


def _detect_label_simplification_columns(df: pd.DataFrame) -> list:
    """Deterministic, no-LLM detector (same discipline as
    _detect_range_columns): a categorical column where
    LABEL_SIMPLIFICATION_MATCH_THRESHOLD or more of its real non-null values
    match the verbose "N [to M] <unit>" shape. Returns
    [{"column", "match_fraction", "sample_value"}, ...]. Never modifies df.
    """
    candidates = []
    for col in df.columns:
        series = df[col].dropna().astype(str).str.strip()
        if series.empty:
            continue
        matches = series.str.match(_VERBOSE_LABEL_RE)
        match_fraction = int(matches.sum()) / len(series)
        if match_fraction >= LABEL_SIMPLIFICATION_MATCH_THRESHOLD:
            candidates.append({
                "column": col,
                "match_fraction": match_fraction,
                "sample_value": series[matches].iloc[0],
            })
    return candidates


LABEL_SIMPLIFICATION_SYSTEM_PROMPT = """You are simplifying the STYLE of one verbose
categorical column, named below, into shorter, equivalent labels — e.g. "51 to 200
employees" -> "51-200", "10000+ employees" -> "10000+". This is NOT a correctness fix
(the verbose form is not wrong) and NOT an enrichment (no new column) — it's a pure
style/readability choice, rewriting the SAME column's values in place.

IMPORTANT — check this first: the verbose pattern was matched mechanically and can be a
false positive. Look at the real sample values given below. If this column does NOT
actually hold a genuinely redundant/verbose label worth shortening, output exactly this
and nothing else:

# NO_SIMPLIFICATION: <one-sentence reason this column should not be simplified>

Do not force a simplification just because the pattern matched. A correct "no
simplification needed" response is a successful outcome, not a failure — you will not be
asked to retry if you decline for a real reason.

If the column DOES genuinely hold verbose labels worth shortening, follow these rules:
- Rewrite the SAME column's values in place — do not add a new column, do not rename the
  column, do not touch any other column.
- Preserve every real distinction between values (a range must stay recognizable as that
  range, e.g. "-" between the two numbers) — never collapse two genuinely different
  values into the same simplified label.
- A value that does NOT match the verbose pattern (a placeholder like "-1"/"Unknown", or
  anything else already short) must be left completely unchanged.
- Output ONLY raw Python code — no explanation, no markdown fences, no backticks —
  UNLESS you are declining, in which case output ONLY the "# NO_SIMPLIFICATION: ..." line
  above.
- The script must read the CSV at the exact path given, rewrite the column's values, and
  write the result back to that same path (overwrite in place).
"""

_NO_SIMPLIFICATION_RE = re.compile(r"^#\s*NO_SIMPLIFICATION:\s*(.+)$", re.IGNORECASE)


def _extract_no_simplification_reason(code: str) -> "str | None":
    """Same discipline as _extract_no_decomposition_reason — returns the
    reason if `code` is exactly a NO_SIMPLIFICATION sentinel response, else
    None."""
    stripped = (code or "").strip()
    match = _NO_SIMPLIFICATION_RE.match(stripped)
    if match and "\n" not in stripped:
        return match.group(1).strip()
    return None


def _generate_label_simplification_code(
    file_path: Path, column: str, candidate: dict, llm,
    previous_code: str = "", previous_error: str = "",
) -> str:
    """Mirrors _generate_range_decomposition_code's shape exactly, but for
    the label-simplification prompt."""
    df = _read_csv_robust(file_path)
    file_context = _describe_file_for_prompt(file_path, df=df)
    match_fraction = candidate.get("match_fraction", 0.0)
    sample_value = candidate.get("sample_value", "")

    human_content = (
        f"File to restyle (read and overwrite this exact path): {file_path}\n\n"
        f"{file_context}\n\n"
        f"The candidate column for label simplification: '{column}'\n"
        f"Mechanically detected: {match_fraction:.0%} of its real non-null values match "
        f"a verbose 'N [to M] <unit>' shape (e.g. {sample_value!r}).\n\n"
        "Inspect the real sample values above for this column before deciding whether "
        "to simplify it or decline."
    )

    hints_path = file_path.parent.parent / "hints.txt"
    if hints_path.exists():
        hints_text = hints_path.read_text().strip()
        if hints_text:
            human_content += (
                f"\n\nDataset-specific context (domain knowledge about this data — "
                f"treat these facts as authoritative when generating code):\n{hints_text}"
            )

    if previous_error:
        human_content += (
            "\n\nA previous attempt at this file's simplification script did not fully "
            f"succeed — fix the script so it actually works:\n{previous_error}"
            f"\n\nPrevious script was:\n{previous_code}"
        )

    response = llm.invoke(
        [
            ("system", LABEL_SIMPLIFICATION_SYSTEM_PROMPT),
            ("human", human_content),
        ]
    )
    content = response.content
    if isinstance(content, list):
        text = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
            if not (isinstance(block, dict) and block.get("type") == "thinking")
        )
    else:
        text = content
    return _strip_code_formatting(text)


def _label_simplification_shape_ok(file_path: Path, original_columns: list, flagged_column: str) -> tuple:
    """Post-fix verification for a style-only in-place rewrite — unlike range
    decomposition/feature derivation (which ADD columns), this must add and
    drop NOTHING: the exact same columns must still be present, and the
    flagged column's values must no longer be MOSTLY verbose (allowing for
    genuinely non-matching values like "-1"/"Unknown" that were never
    supposed to change). Returns (ok, reason).
    """
    try:
        current_df = _read_csv_robust(file_path)
    except Exception as e:
        return False, f"Could not re-read the file after the fix: {e}"

    current_columns = list(current_df.columns)
    if current_columns != original_columns:
        return False, (
            f"Expected the exact same columns as before (a style-only rewrite adds/drops "
            f"no columns), found {current_columns} vs original {original_columns}."
        )
    if flagged_column not in current_df.columns:
        return False, f"Column '{flagged_column}' is missing after the fix."

    series = current_df[flagged_column].dropna().astype(str).str.strip()
    if series.empty:
        return False, f"Column '{flagged_column}' has no real values after the fix."
    still_verbose_fraction = series.str.match(_VERBOSE_LABEL_RE).mean()
    if still_verbose_fraction > (1 - LABEL_SIMPLIFICATION_MATCH_THRESHOLD):
        return False, (
            f"Expected the verbose pattern mostly replaced, but "
            f"{still_verbose_fraction:.0%} of values still match the original verbose shape."
        )
    return True, ""


def simplify_labels(file_path: Path, candidate: dict, llm=None) -> dict:
    """Opt-in style-only label simplification for ONE candidate column (see
    _detect_label_simplification_columns). Own generate -> approve -> execute
    -> re-check cycle, entirely separate from clean_dataset()'s fail/warn
    loop and from range decomposition/feature derivation's add-a-column
    shape — this REWRITES the flagged column's values in place, adding
    nothing. NEVER called automatically. Reuses the exact same
    _request_approval/_execute_cleaning_code as every other fix in this
    pipeline.

    Returns {"column", "status", "attempts", "error", "generated_code"};
    status uses the same vocabulary as decompose_range_column's, including
    "declined_false_positive" for a legitimate NO_SIMPLIFICATION decline —
    checked immediately after generation, BEFORE any approval prompt or
    execution, on every attempt (Spec 2.1's retry-pressure lesson, applied
    here from the start).
    """
    column = candidate["column"]
    if llm is None:
        from utils.llm_pick import pick_llm
        llm = pick_llm("high")

    try:
        original_columns = list(_read_csv_robust(file_path).columns)
    except Exception as e:
        return {
            "column": column, "status": "skipped_failed", "attempts": 0,
            "error": f"Could not read {file_path} to establish a baseline: {e}",
            "generated_code": "",
        }

    previous_code = ""
    previous_error = ""
    attempt = 0
    last_code = ""

    while attempt < MAX_CLEAN_ATTEMPTS:
        attempt += 1
        code = _generate_label_simplification_code(
            file_path, column, candidate, llm, previous_code, previous_error
        )

        no_simplification_reason = _extract_no_simplification_reason(code)
        if no_simplification_reason is not None:
            return {
                "column": column, "status": "declined_false_positive", "attempts": attempt,
                "error": no_simplification_reason, "generated_code": "",
            }

        approved = _request_approval(code, file_path)
        if not approved:
            return {
                "column": column, "status": "skipped_declined", "attempts": attempt,
                "error": "", "generated_code": "",
            }

        success, error = _execute_cleaning_code(code, file_path)
        if not success:
            previous_code, previous_error = code, error
            last_code = code
            if attempt >= MAX_CLEAN_ATTEMPTS:
                return {
                    "column": column, "status": "skipped_failed", "attempts": attempt,
                    "error": error, "generated_code": last_code,
                }
            continue

        last_code = code
        shape_ok, shape_reason = _label_simplification_shape_ok(file_path, original_columns, column)
        if shape_ok:
            return {
                "column": column, "status": "resolved", "attempts": attempt,
                "error": "", "generated_code": last_code,
            }

        previous_code = code
        previous_error = shape_reason
        if attempt >= MAX_CLEAN_ATTEMPTS:
            return {
                "column": column, "status": "skipped_incomplete", "attempts": attempt,
                "error": shape_reason, "generated_code": last_code,
            }

    return {
        "column": column, "status": "skipped_incomplete", "attempts": attempt,
        "error": previous_error, "generated_code": last_code,
    }


def _extract_reasoning_comments(code: str) -> list:
    """Pull the # comment lines from a generated cleaning script — these are the LLM's
    reasoning about WHY each transformation was applied, not just what it did."""
    return [line.strip() for line in code.splitlines() if line.strip().startswith("#")]


def _append_cleaning_log(result: "CleaningResult", source_folder: str, trigger: str) -> None:
    """Append one entry to logs/cleaning_log.jsonl capturing this clean_dataset() run.
    Never raises — a logging failure must not abort a successful cleaning run.
    """
    try:
        files = []
        all_processed = list(result.cleaned_files) + list(result.skipped_files)
        for rec in all_processed:
            table_name = Path(rec.file_name).stem

            issues_found = [
                {"issue": i, "severity": _issue_severity(i)} for i in rec.issues
            ]

            fail_entries = []
            for fir in rec.fail_issue_records:
                fail_entries.append({
                    "issue": fir.issue,
                    "status": fir.status,
                    "reasoning_comments": _extract_reasoning_comments(fir.generated_code),
                })

            fail_batch_entries = []
            for fbr in rec.fail_batch_records:
                fail_batch_entries.append({
                    "issues": fbr.issues,
                    "signature": _signature_to_jsonable(fbr.signature),
                    "status": fbr.status,
                    "reasoning_comments": _extract_reasoning_comments(fbr.generated_code),
                })

            warn_batch_entries = []
            for wb in rec.warn_batches:
                warn_batch_entries.append({
                    "issues": wb.issues,
                    "status": wb.status,
                    "reasoning_comments": _extract_reasoning_comments(wb.generated_code),
                })

            issues_resolved = [i for i in rec.issues if i not in (rec.remaining_issues or [])]
            files.append({
                "file_name": rec.file_name,
                "table_name": table_name,
                "status": rec.status,
                "issues_found": issues_found,
                "row_count_before": rec.row_count_before,
                "row_count_after": rec.row_count_after,
                "row_loss_flagged": rec.row_loss_flagged,
                "issues_resolved": issues_resolved,
                "issues_still_unresolved": list(rec.remaining_issues or []),
                "fail_issues": fail_entries,
                "fail_batches": fail_batch_entries,
                "warn_batches": warn_batch_entries,
            })

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source_folder": source_folder,
            "trigger": trigger,
            "files": files,
        }

        _CLEANING_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_CLEANING_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def clean_dataset(folder_path, llm=None, trigger: str = "manual", recipe_conn=None) -> CleaningResult:
    """Process every top-level CSV in folder_path SEPARATELY: run the rubric per file,
    split its issues into fail-level and warn-level (see FAIL_LEVEL_PREFIXES /
    WARN_LEVEL_PREFIXES), and for any file with real issues:

    1. Fail-level issues are first grouped by treatment signature (Spec 4 —
       _group_issues_by_signature / _issue_treatment_signature): a group of 2+
       issues sharing an identical, mechanically-verified fix requirement (e.g. the
       same placeholder tokens, or the same currency symbol, in different columns)
       gets ONE combined generate -> approve -> execute -> re-check cycle
       (-> FailBatchRecord); everything else — no defined signature, or a signature
       that happens to be unique among this file's fail issues — is still processed
       individually, one at a time, in the order check_rubric() found them, exactly
       as before (-> IssueCleaningRecord). Either way it's the same
       generate -> approve -> execute -> immediate re-check -> retry cycle, capped at
       MAX_CLEAN_ATTEMPTS for that issue/group specifically (see _clean_issue_group).
       If one fail-level issue/group is declined, the rest of the file's processing
       (remaining fail-level issues/groups, the warn-level batches, the final check)
       is skipped entirely — same decline semantics as the original whole-file
       design. If a fail-level issue/group is NOT declined but still can't be
       resolved (a real execution error every attempt, or still detectably present
       every attempt), it's recorded as skipped and processing moves on to the next
       one — one unresolved issue/group never aborts the rest of the file.
    2. Once every fail-level issue/group has been processed (and none were
       declined), warn-level issues are similarly grouped by signature
       (_partition_warn_groups) — but preserving the pre-Spec-4 default: any group of
       2+ issues sharing a real signature gets its own combined cycle
       (-> its own WarnBatchRecord), while everything else is still pooled into ONE
       final combined batch, exactly the original "always one whole-file warn batch"
       behavior for anything that isn't a genuine multi-issue signature match. A file
       can therefore now have MULTIPLE WarnBatchRecords, not just one.
    3. Run a final, full check_rubric() pass across the complete result (unless
       something above was declined) and compare row counts before vs. after — exactly
       the same post-cleaning validation and row-count-loss check as before this
       restructuring, just now describing the outcome of the fail-then-warn pipeline
       instead of one undifferentiated retry loop.

    Files with nothing flagged are left alone entirely: no clone, no LLM call, no
    approval prompt.

    llm: optional injected chat model (used by tests to deterministically force a
    failing-then-succeeding code-gen sequence); defaults to pick_llm("high") in real
    use — this is a dependency default, not a safety bypass: the approval gate always
    uses the real input() builtin regardless of what llm is passed, so no caller can
    construct a call that skips it.

    recipe_conn (Spec 7, optional, default None): when given, a previously-approved
    fix for a signature-eligible fail/warn group on this exact table is replayed
    without a new LLM call or approval prompt (still fully re-verified — see
    _clean_issue_group). None (the default) reproduces this function's exact
    pre-Spec-7 behavior with zero database dependency, which is load-bearing:
    clean_data.py's whole point is running with no database connection at all,
    and must keep working exactly as before.
    """
    from utils.llm_pick import pick_llm  # local import: keeps this module usable without

    # requiring the LLM stack (e.g. for callers that only need check_rubric) to import
    # cleanly, and avoids a module-load-time dependency on ANTHROPIC_API_KEY being set.

    folder = Path(folder_path)
    cleaned_dir = folder / "cleaned"
    csv_files = sorted(p for p in folder.glob("*.csv") if p.is_file())

    result = CleaningResult(folder_path=str(folder), cleaned_dir=str(cleaned_dir))

    for file_path in csv_files:
        static_issues = check_rubric(file_path)
        resolved_llm = llm if llm is not None else pick_llm("high")

        # Spec 7: only computed when a recipe cache is actually in play — same
        # local-import discipline as pick_llm above, and avoids sanitize_identifier
        # needing to be a hard top-level dependency of this module (utils.load_data
        # already imports FROM utils.data_cleaning; a top-level import the other way
        # would risk a real circular import, not just an unused-dependency style issue).
        recipe_table_name = None
        if recipe_conn is not None:
            from utils.load_data import sanitize_identifier

            recipe_table_name = sanitize_identifier(file_path.stem)

        # Discovery phase (see explore_and_verify): runs even when the static
        # rubric found nothing for this file — that's exactly the case a closed
        # catalog of pattern-matchers can miss entirely (see its module docstring).
        # Every discovered issue is mechanically verified against the real full
        # column before it's allowed to reach this list, so it merges onto
        # static_issues before severity splitting with no new code path below.
        try:
            exploration_df = _read_csv_robust(file_path)
        except Exception:
            exploration_df = None
        discovered_issues, pattern_lookup = explore_and_verify(
            exploration_df, llm=resolved_llm,
            flagged_columns=_columns_with_fail_issues(static_issues),
        )
        issues = static_issues + discovered_issues

        if not issues:
            result.untouched_files.append(file_path.name)
            continue

        cloned_path = _clone_file(file_path, cleaned_dir)
        row_count_before = _count_csv_rows(cloned_path)
        fail_issues, warn_issues = _split_issues_by_severity(issues)

        total_attempts = 0
        fail_issue_records: list = []
        fail_batch_records: list = []
        warn_batches: list = []
        declined = False

        # Step 2: fail-level issues, grouped by treatment signature first (Spec 4) —
        # a group of 2+ issues sharing an identical, mechanically-verified signature
        # (see _issue_treatment_signature) gets ONE combined generate/approve/execute/
        # re-check cycle (-> FailBatchRecord); everything else (no signature, or a
        # signature unique among this file's fail issues) is still processed exactly
        # as before — its own cycle, one at a time (-> IssueCleaningRecord), in the
        # order check_rubric() found them.
        for group in _group_issues_by_signature(fail_issues, df=exploration_df):
            group_signature = _issue_treatment_signature(group[0], df=exploration_df)
            status, attempts, error, remaining, code = _clean_issue_group(
                cloned_path, group, resolved_llm, pattern_lookup=pattern_lookup,
                recipe_conn=recipe_conn, table_name=recipe_table_name, signature=group_signature,
            )
            total_attempts += attempts
            if len(group) == 1:
                fail_issue_records.append(
                    IssueCleaningRecord(
                        issue=group[0], status=status, attempts=attempts, error=error, generated_code=code
                    )
                )
            else:
                fail_batch_records.append(
                    FailBatchRecord(
                        issues=group,
                        signature=group_signature,
                        status=status,
                        attempts=attempts,
                        error=error,
                        remaining_issues=remaining,
                        generated_code=code,
                    )
                )
            if status == "skipped_declined":
                declined = True
                break
            # skipped_failed / skipped_incomplete: report and continue to the next
            # fail-level issue/group — one unresolved issue/group never aborts the
            # rest of the file.

        # Step 3: warn-level issues, similarly grouped by signature (Spec 4), but
        # preserving the pre-Spec-4 default: anything NOT part of a genuine 2+-issue
        # signature match is still pooled into one final combined batch, exactly the
        # original "always one whole-file warn batch" behavior — see
        # _partition_warn_groups. Skipped entirely if a fail-level issue was declined
        # above.
        if not declined:
            warn_groups = _partition_warn_groups(warn_issues, df=exploration_df)
            if warn_groups:
                for group in warn_groups:
                    group_signature = _issue_treatment_signature(group[0], df=exploration_df)
                    status, attempts, error, remaining, code = _clean_issue_group(
                        cloned_path, group, resolved_llm,
                        recipe_conn=recipe_conn, table_name=recipe_table_name, signature=group_signature,
                    )
                    total_attempts += attempts
                    warn_batches.append(
                        WarnBatchRecord(
                            issues=group,
                            status=status,
                            attempts=attempts,
                            error=error,
                            remaining_issues=remaining,
                            generated_code=code,
                        )
                    )
                    if status == "skipped_declined":
                        declined = True
                        break
            else:
                warn_batches = [WarnBatchRecord(issues=[], status="no_warn_issues")]

        # Spec 3: purely informational scan for structured-decomposition candidates
        # (e.g. a salary/revenue range column) against the file's REAL current
        # state, regardless of outcome (declined/incomplete/cleaned) — see
        # _detect_range_columns and FileCleaningRecord.structured_decomposition_
        # candidates. Never modifies the file, never triggers a fix; a read
        # failure here must never abort cleaning over an enrichment side-scan.
        try:
            structured_decomposition_candidates = _detect_range_columns(_read_csv_robust(cloned_path))
        except Exception:
            structured_decomposition_candidates = []

        if declined:
            result.skipped_files.append(
                FileCleaningRecord(
                    file_name=file_path.name,
                    issues=issues,
                    fail_issue_records=fail_issue_records,
                    fail_batch_records=fail_batch_records,
                    warn_batches=warn_batches,
                    status="skipped_declined",
                    attempts=total_attempts,
                    structured_decomposition_candidates=structured_decomposition_candidates,
                )
            )
            continue

        # Step 4: final, full check_rubric() pass across the complete result, plus the
        # row-count-loss comparison against the original — the same post-cleaning
        # validation this module already had, now describing the outcome of the
        # fail-then-warn pipeline as a whole rather than one undifferentiated retry loop.
        post_issues = check_rubric(cloned_path)
        still_present = [issue for issue in issues if issue in post_issues]
        row_count_after = _count_csv_rows(cloned_path)
        row_loss_flagged = False
        if row_count_before and row_count_after is not None:
            loss_frac = (row_count_before - row_count_after) / row_count_before
            row_loss_flagged = loss_frac >= ROW_LOSS_FLAG_THRESHOLD

        file_record = FileCleaningRecord(
            file_name=file_path.name,
            issues=issues,
            fail_issue_records=fail_issue_records,
            fail_batch_records=fail_batch_records,
            warn_batches=warn_batches,
            attempts=total_attempts,
            rubric_recheck_passed=not still_present,
            remaining_issues=still_present,
            row_count_before=row_count_before,
            row_count_after=row_count_after,
            row_loss_flagged=row_loss_flagged,
            structured_decomposition_candidates=structured_decomposition_candidates,
        )
        if still_present:
            file_record.status = "skipped_incomplete"
            result.skipped_files.append(file_record)
        else:
            file_record.status = "cleaned"
            result.cleaned_files.append(file_record)

    _append_cleaning_log(result, str(folder), trigger)
    return result
