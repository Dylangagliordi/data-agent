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


def _check_duplicates(df: pd.DataFrame) -> list:
    issues = []
    dup_row_count = df.duplicated().sum()
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
    column (e.g. '$50.00', '50 kg' stored as text instead of a plain number)."""
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
            issues.append(
                f"Currency/unit symbols: column '{col}' has {total_matches} value(s) with "
                "a currency symbol or unit suffix embedded in an otherwise numeric value."
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
    long-form prose (see _is_long_form_prose_column), has no non-null values at
    all, or has too few real values to draw any real conclusion from (see
    _EXPLORE_MIN_COLUMN_ROWS — a tiny column makes any regex a tautology, not
    evidence). The other skip condition from the spec — a column check_rubric
    already flagged with a fail-level issue this run — is applied by the
    caller (explore_and_verify), which is where that information actually
    lives.
    """
    series = df[column]
    non_null = series.dropna()
    if (
        len(non_null) < _EXPLORE_MIN_COLUMN_ROWS
        or non_null.empty
        or _is_long_form_prose_column(series)
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


def _verify_hypothesis(df: pd.DataFrame, column: str, hypothesis: str, llm=None) -> "str | None":
    """The trust boundary: converts one loose hypothesis into a concrete regex +
    threshold via the LLM, then checks that regex against EVERY non-null value in
    the real column (not the sample) with plain Python/pandas — no LLM involved
    in the actual verification step.

    Returns None (hypothesis discarded, never escalated) when:
    - the column has too few real values to meaningfully verify against (see
      _EXPLORE_MIN_COLUMN_ROWS — defense in depth alongside explore_column's
      own skip, in case this is ever called directly with a hypothesis from
      elsewhere);
    - the LLM call fails, or its proposed pattern doesn't compile as a regex;
    - the real match fraction against the full column is below the proposed
      match_threshold — this hypothesis didn't hold up against the full data.

    Returns a formatted issue string, in exactly the shape check_rubric()'s other
    checks use and tagged "(discovered)" so it's visibly distinguishable in logs/
    reports, when the hypothesis is mechanically confirmed.
    """
    series = df[column]
    non_null = series.dropna().astype(str)
    if len(non_null) < _EXPLORE_MIN_COLUMN_ROWS:
        return None

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
        return None

    try:
        compiled_pattern = re.compile(proposal.pattern)
    except re.error:
        return None

    matches = non_null.map(lambda v: bool(compiled_pattern.search(v)))
    match_count = int(matches.sum())
    match_frac = match_count / len(non_null)

    if match_frac < proposal.match_threshold:
        return None

    return (
        f"Composite field (discovered): column '{column}' has {match_count} value(s) "
        f"({match_frac:.0%}) matching the pattern '{proposal.pattern}' — this looks like two "
        f"distinct values glued together, not caught by a fixed rubric check."
    )


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


def explore_and_verify(df: "pd.DataFrame | None", llm=None, flagged_columns: "set | None" = None) -> list:
    """Orchestrator: runs explore_column across every eligible column, verifies
    every returned hypothesis via _verify_hypothesis, runs _explore_column_pairs,
    and returns a flat list of issue strings in the exact format check_rubric()
    produces (so they merge into the same fail/warn pipeline with no new code
    path).

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
        return []

    if llm is None:
        from utils.llm_pick import pick_llm
        llm = pick_llm("cheap")

    flagged_columns = flagged_columns or set()
    issues: list = []
    call_count = 0

    eligible_columns = [
        col for col in df.columns
        if col not in flagged_columns and not _is_long_form_prose_column(df[col])
    ]

    for col in eligible_columns:
        if call_count >= EXPLORE_MAX_LLM_CALLS_PER_TABLE:
            print(
                f"[explore] LLM call ceiling ({EXPLORE_MAX_LLM_CALLS_PER_TABLE}) reached "
                f"before exploring column '{col}' — stopping discovery early for this table.",
                file=sys.stderr,
            )
            return issues

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
                return issues
            verified = _verify_hypothesis(df, col, hypothesis, llm=llm)
            call_count += 1
            if verified:
                issues.append(verified)

    remaining_budget = EXPLORE_MAX_LLM_CALLS_PER_TABLE - call_count
    if remaining_budget <= 0:
        print(
            f"[explore] LLM call ceiling ({EXPLORE_MAX_LLM_CALLS_PER_TABLE}) reached — "
            "skipping column-pair exploration for this table.",
            file=sys.stderr,
        )
        return issues

    pair_limit = min(EXPLORE_MAX_COLUMN_PAIRS, remaining_budget)
    issues.extend(_explore_column_pairs(df, llm=llm, max_pairs=pair_limit))
    return issues


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
    specific issue was still detected immediately afterward, every attempt).
    """

    issue: str
    status: str = ""
    attempts: int = 0
    error: str = ""
    generated_code: str = ""


@dataclass
class WarnBatchRecord:
    """The batched outcome of every warn-level issue in a file, processed together in
    one combined generate -> approve -> execute -> re-check cycle — unchanged in
    spirit from the original whole-file design, just scoped to warn-level issues only.

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
class FileCleaningRecord:
    """One file's outcome from clean_dataset(): either cleaned, or skipped.

    issues: every issue check_rubric() originally found for this file (fail-level +
    warn-level together, in the order check_rubric() produced them) — the same full
    list previous versions of this dataclass stored here, kept for anyone (tests,
    callers) that only cares about "what was wrong with this file", not how each
    issue was individually processed.

    fail_issue_records: list[IssueCleaningRecord], one per fail-level issue, in the
    order they were processed (== the order check_rubric() found them) — empty if
    this file had no fail-level issues at all.

    warn_batch: WarnBatchRecord for the batched warn-level pass, or None if the file's
    processing was declined before ever reaching the warn-level stage (mid-way through
    the fail-level loop).

    status / attempts / error: an aggregate/overall view for this file — status is
    "cleaned" (final full re-check passed cleanly), "skipped_declined" (the user
    declined an approval prompt for some issue/batch — processing of the REST of the
    file stops at that point, same as the original whole-file design's decline
    semantics), or "skipped_incomplete" (every issue/batch got its full, individually-
    scoped chance, but the final full check_rubric() pass still found at least one of
    the file's original issues present). attempts is the sum of every fail-issue's and
    the warn-batch's individual attempt counts, for a quick "how much retrying did
    this file need in total" figure.

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
    """

    file_name: str
    issues: list = field(default_factory=list)
    fail_issue_records: list = field(default_factory=list)
    warn_batch: WarnBatchRecord | None = None
    status: str = ""  # "cleaned" | "skipped_declined" | "skipped_incomplete"
    attempts: int = 0
    error: str = ""
    rubric_recheck_passed: bool = True
    remaining_issues: list = field(default_factory=list)
    row_count_before: int | None = None
    row_count_after: int | None = None
    row_loss_flagged: bool = False


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
    EXCEPT ones individually confirmed "resolved" before the decline point (a
    fail-level issue with its own IssueCleaningRecord.status == "resolved", or every
    warn-level issue if the whole warn batch resolved) counts as still unresolved.
    """
    if rec.status != "skipped_declined":
        return rec.remaining_issues

    resolved_fail_issues = {r.issue for r in rec.fail_issue_records if r.status == "resolved"}
    warn_batch_resolved = rec.warn_batch is not None and rec.warn_batch.status == "resolved"
    warn_issue_set = set(rec.warn_batch.issues) if rec.warn_batch is not None else set()

    unresolved = []
    for issue in rec.issues:
        if issue in resolved_fail_issues:
            continue
        if warn_batch_resolved and issue in warn_issue_set:
            continue
        unresolved.append(issue)
    return unresolved


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
        the warn-batch outcome, and the final overall check — used for both cleaned
        and skipped files so the same real information is visible either way."""
        lines = []
        if rec.fail_issue_records:
            lines.append("        Fail-level issues (processed individually):")
            for issue_rec in rec.fail_issue_records:
                outcome = _issue_outcome_line(issue_rec.status, issue_rec.attempts, issue_rec.error)
                lines.append(f"          * {issue_rec.issue}")
                lines.append(f"              -> {outcome}")
        if rec.warn_batch is not None and rec.warn_batch.status != "no_warn_issues":
            lines.append("        Warn-level issues (batched together):")
            for issue in rec.warn_batch.issues:
                lines.append(f"          * {issue}")
            outcome = _issue_outcome_line(rec.warn_batch.status, rec.warn_batch.attempts, rec.warn_batch.error)
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
    too-high cardinality to be categorical, so the caller can skip it cleanly."""
    if col not in df.columns:
        return ""
    series = df[col].dropna()
    if series.empty or pd.api.types.is_numeric_dtype(series):
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


def _issue_guidance(issue: str, df=None) -> str:
    """Extra, deterministic guidance appended under one issue line in the code-gen prompt.

    Fires for "Missing values" issues: parses the real percentage from check_rubric's own
    issue string so the LLM gets a concrete number.  When df is provided and the column is
    categorical, also appends a fill-value recommendation (mode vs 'Unknown') based on the
    actual value distribution."""
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
                advice = _categorical_fill_advice(df, col_match.group(1))
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


def _clean_issue_group(cloned_path: Path, target_issues: list, llm) -> tuple:
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

    Returns (status, attempts, error, remaining_issues, last_code):
    - status: "resolved" | "skipped_declined" | "skipped_failed" | "skipped_incomplete"
    - attempts: how many attempts this group actually took
    - error: the real last error/still-present description (empty if resolved/declined)
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

    while attempt < MAX_CLEAN_ATTEMPTS:
        attempt += 1
        code = _generate_cleaning_code(cloned_path, remaining_issues, llm, previous_code, previous_error)
        approved = _request_approval(code, cloned_path)
        if not approved:
            return "skipped_declined", attempt, "", [], ""

        success, error = _execute_cleaning_code(code, cloned_path)
        if not success:
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

        if not still_present and not row_count_changed:
            return "resolved", attempt, "", [], last_executed_code

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

            warn_entry = None
            if rec.warn_batch is not None:
                warn_entry = {
                    "issues": rec.warn_batch.issues,
                    "status": rec.warn_batch.status,
                    "reasoning_comments": _extract_reasoning_comments(rec.warn_batch.generated_code),
                }

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
                "warn_batch": warn_entry,
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


def clean_dataset(folder_path, llm=None, trigger: str = "manual") -> CleaningResult:
    """Process every top-level CSV in folder_path SEPARATELY: run the rubric per file,
    split its issues into fail-level and warn-level (see FAIL_LEVEL_PREFIXES /
    WARN_LEVEL_PREFIXES), and for any file with real issues:

    1. Process EACH fail-level issue individually, one at a time, in the order
       check_rubric() found them: its own generate -> approve -> execute -> immediate
       re-check (scoped to just that one issue) -> retry cycle, capped at
       MAX_CLEAN_ATTEMPTS for that issue specifically (see _clean_issue_group). If one
       fail-level issue is declined, the rest of the file's processing (remaining
       fail-level issues, the warn-level batch, the final check) is skipped entirely —
       same decline semantics as the original whole-file design. If a fail-level issue
       is NOT declined but still can't be resolved (a real execution error every
       attempt, or the issue's still detectably present every attempt), it's recorded
       as skipped for that one issue and processing moves on to the next fail-level
       issue — one unresolved issue never aborts the rest of the file.
    2. Once every fail-level issue has been individually processed (and none were
       declined), batch every warn-level issue into ONE combined generate -> approve
       -> execute -> re-check cycle, same as the original whole-file design, just
       scoped to warn-level issues only.
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
        discovered_issues = explore_and_verify(
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
        warn_batch: WarnBatchRecord | None = None
        declined = False

        # Step 2: each fail-level issue gets its OWN generate/approve/execute/re-check
        # cycle, one at a time, in the order check_rubric() found them — never batched
        # together with the others.
        for issue in fail_issues:
            status, attempts, error, _remaining, code = _clean_issue_group(cloned_path, [issue], resolved_llm)
            total_attempts += attempts
            fail_issue_records.append(
                IssueCleaningRecord(issue=issue, status=status, attempts=attempts, error=error, generated_code=code)
            )
            if status == "skipped_declined":
                declined = True
                break
            # skipped_failed / skipped_incomplete: report and continue to the next
            # fail-level issue — one unresolved issue never aborts the rest of the file.

        # Step 3: every warn-level issue, batched into one combined cycle — unchanged
        # from the original whole-file design, just scoped to warn-level issues only.
        # Skipped entirely if a fail-level issue was declined above.
        if not declined:
            if warn_issues:
                status, attempts, error, remaining, code = _clean_issue_group(cloned_path, warn_issues, resolved_llm)
                total_attempts += attempts
                warn_batch = WarnBatchRecord(
                    issues=warn_issues,
                    status=status,
                    attempts=attempts,
                    error=error,
                    remaining_issues=remaining,
                    generated_code=code,
                )
                if status == "skipped_declined":
                    declined = True
            else:
                warn_batch = WarnBatchRecord(issues=[], status="no_warn_issues")

        if declined:
            result.skipped_files.append(
                FileCleaningRecord(
                    file_name=file_path.name,
                    issues=issues,
                    fail_issue_records=fail_issue_records,
                    warn_batch=warn_batch,
                    status="skipped_declined",
                    attempts=total_attempts,
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
            warn_batch=warn_batch,
            attempts=total_attempts,
            rubric_recheck_passed=not still_present,
            remaining_issues=still_present,
            row_count_before=row_count_before,
            row_count_after=row_count_after,
            row_loss_flagged=row_loss_flagged,
        )
        if still_present:
            file_record.status = "skipped_incomplete"
            result.skipped_files.append(file_record)
        else:
            file_record.status = "cleaned"
            result.cleaned_files.append(file_record)

    _append_cleaning_log(result, str(folder), trigger)
    return result
