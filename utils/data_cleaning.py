"""
Shared data-cleaning core, used by clean_data.py, the enhanced load_data.py, and the
ETL analyst's transform_load tool — one real implementation, not three copies.

Piece 1 (this section): a deterministic, no-LLM rubric checker. Given a single raw file,
inspect it against the rubric from the spec and return a list of concrete issue strings
(empty list == "nothing flagged, leave this file alone"). This never touches the LLM and
never modifies anything on disk — it only reads and reports.

Rubric checked here, per file:
- Missing values beyond a reasonable threshold in columns that matter
- Duplicate rows, or duplicate values in a column that should be unique
- Wrong data types (numbers stored as text, inconsistent date formats)
- Inconsistent categorical values (the same real value written different ways)
- Formatting noise (stray whitespace, inconsistent capitalization)
- Invalid or clearly-impossible values (negative counts, out-of-range dates)
- Encoding problems (garbled or mixed character encoding)
- Structural issues (inconsistent column counts, malformed rows)

Deliberately NOT checked here: fan-out (multiple rows per a foreign key relative to
another table). That's a cross-table relationship only checkable via information_schema
once data is already loaded into Postgres, and it's already handled live by
agents/sql_analyst.py's add_context / _detect_fanout_warnings. Duplicating it here
against raw, not-yet-loaded files would be redundant and would need its own (different)
implementation since there's no foreign-key structure to check against pre-load.
"""

import csv
import shutil
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

# Missing-values threshold: a column with more than this fraction of nulls/blank is
# flagged. 5% is a deliberately conservative default — real data almost always has a
# few genuinely missing values; this is meant to catch columns that are substantially
# incomplete, not to flag every dataset with a handful of nulls.
MISSING_VALUE_THRESHOLD = 0.05

# Column-name substrings that suggest a column should hold non-negative counts/quantities/
# amounts — used only for the "impossible negative value" check. This is a heuristic on
# naming convention (like generate_sql's fan-out FK heuristic), not a hardcoded list of
# real dataset column names.
NON_NEGATIVE_NAME_HINTS = ("count", "qty", "quantity", "amount", "price", "value", "total", "age")

# Column-name substrings that suggest a column is meant to be a unique identifier for a
# row in this file (e.g. "id", "_id", "code") — used only for the duplicate-unique-value
# check. Again a naming heuristic, not hardcoded per-dataset names.
UNIQUE_ID_NAME_HINTS = ("id", "code", "key")


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
    issues += _check_encoding(raw_bytes)
    issues += _check_structural(path, raw_bytes)

    try:
        df = pd.read_csv(path, dtype=str, keep_default_na=True, on_bad_lines="skip")
    except Exception as e:
        issues.append(f"Structural issue: file could not be parsed as CSV at all: {e}")
        return issues

    issues += _check_missing_values(df)
    issues += _check_duplicates(df)
    issues += _check_dtypes(df)
    issues += _check_categorical_inconsistency(df)
    issues += _check_formatting_noise(df)
    issues += _check_impossible_values(df)

    return issues


# ---------------------------------------------------------------------------
# Piece 2: LLM-generated cleaning code, human approval gate, execution with
# retry-on-real-error, and the clean_dataset() orchestrator that ties it all
# together. This is the part clean_data.py, the enhanced load_data.py, and the
# ETL analyst's transform_load tool all call into — one implementation.
# ---------------------------------------------------------------------------

MAX_CLEAN_ATTEMPTS = 3


@dataclass
class FileCleaningRecord:
    """One file's outcome from clean_dataset(): either cleaned, or skipped."""

    file_name: str
    issues: list = field(default_factory=list)
    status: str = ""  # "cleaned" | "skipped_declined" | "skipped_failed"
    attempts: int = 0
    error: str = ""


@dataclass
class CleaningResult:
    """Summary of a whole clean_dataset() run across every file in a folder."""

    folder_path: str
    cleaned_dir: str
    untouched_files: list = field(default_factory=list)  # filenames needing no cleaning
    cleaned_files: list = field(default_factory=list)  # list[FileCleaningRecord], status="cleaned"
    skipped_files: list = field(default_factory=list)  # list[FileCleaningRecord], skipped

    def summary(self) -> str:
        lines = [f"Cleaning summary for {self.folder_path}:"]
        lines.append(
            f"  Untouched (no issues found): {len(self.untouched_files)} "
            f"({', '.join(self.untouched_files) or 'none'})"
        )
        if self.cleaned_files:
            lines.append(f"  Cleaned successfully: {len(self.cleaned_files)}")
            for rec in self.cleaned_files:
                lines.append(f"    - {rec.file_name} (attempts: {rec.attempts})")
                for issue in rec.issues:
                    lines.append(f"        * {issue}")
        else:
            lines.append("  Cleaned successfully: 0")
        if self.skipped_files:
            lines.append(f"  Skipped: {len(self.skipped_files)}")
            for rec in self.skipped_files:
                lines.append(f"    - {rec.file_name} ({rec.status}): {rec.error or 'declined by user'}")
        if self.cleaned_files:
            lines.append(f"  Cleaned output folder: {self.cleaned_dir}")
        return "\n".join(lines)


def _clone_file(file_path: Path, cleaned_dir: Path) -> Path:
    """Copy file_path into cleaned_dir (creating it if needed) and return the clone's
    path. The raw file at file_path is never opened for writing anywhere in this module."""
    cleaned_dir.mkdir(parents=True, exist_ok=True)
    dest = cleaned_dir / file_path.name
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
- Only address the specific issues given — do not "fix" anything not listed.
- Import any libraries you use (e.g. `import pandas as pd`) — nothing is pre-imported for you.
- The script will be executed exactly as returned, top to bottom, standalone."""


def _describe_file_for_prompt(file_path: Path) -> str:
    """Build a real-sample-rows + real-column/dtype context block for one file, the same
    principle generate_sql's schema context uses: concrete data, not a generic instruction."""
    df = pd.read_csv(file_path, dtype=str, keep_default_na=True, on_bad_lines="skip")
    col_info = "\n".join(f"  - {c}" for c in df.columns)
    sample = df.head(5)
    sample_lines = "\n".join(str(row.to_dict()) for _, row in sample.iterrows())
    return f"Columns:\n{col_info}\n\nSample rows (real, from this file):\n{sample_lines}"


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
    file_context = _describe_file_for_prompt(file_path)
    issue_lines = "\n".join(f"- {issue}" for issue in issues)
    human_content = (
        f"File to clean (read and overwrite this exact path): {file_path}\n\n"
        f"{file_context}\n\n"
        f"Specific issues found in THIS file (fix only these):\n{issue_lines}"
    )
    if previous_error:
        human_content += (
            "\n\nA previous attempt at this file's cleaning script failed with this real "
            f"error when executed — fix the script so it actually works:\n{previous_error}"
            f"\n\nPrevious (failed) script was:\n{previous_code}"
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
    place a cleaning script gets a chance to run, for every caller, every time."""
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


def clean_dataset(folder_path, llm=None) -> CleaningResult:
    """Process every top-level CSV in folder_path SEPARATELY: run the rubric per file,
    and for any file with real issues, clone -> generate -> approve -> execute -> retry
    (up to MAX_CLEAN_ATTEMPTS) -> skip-and-report on exhaustion, continuing with the rest
    of the folder either way. Files with nothing flagged are left alone entirely: no
    clone, no LLM call, no approval prompt.

    llm: optional injected chat model (used by tests to deterministically force a
    failing-then-succeeding code-gen sequence for the retry-cap test); defaults to
    pick_llm("high") in real use — this is a dependency default, not a safety bypass:
    the approval gate below always uses the real input() builtin regardless of what llm
    is passed, so no caller can construct a call that skips it.
    """
    from utils.llm_pick import pick_llm  # local import: keeps this module usable without

    # requiring the LLM stack (e.g. for callers that only need check_rubric) to import
    # cleanly, and avoids a module-load-time dependency on ANTHROPIC_API_KEY being set.

    folder = Path(folder_path)
    cleaned_dir = folder / "cleaned"
    csv_files = sorted(p for p in folder.glob("*.csv") if p.is_file())

    result = CleaningResult(folder_path=str(folder), cleaned_dir=str(cleaned_dir))

    for file_path in csv_files:
        issues = check_rubric(file_path)
        if not issues:
            result.untouched_files.append(file_path.name)
            continue

        cloned_path = _clone_file(file_path, cleaned_dir)
        resolved_llm = llm if llm is not None else pick_llm("high")

        previous_code = ""
        previous_error = ""
        attempt = 0
        while attempt < MAX_CLEAN_ATTEMPTS:
            attempt += 1
            code = _generate_cleaning_code(
                cloned_path, issues, resolved_llm, previous_code, previous_error
            )
            approved = _request_approval(code, cloned_path)
            if not approved:
                result.skipped_files.append(
                    FileCleaningRecord(
                        file_name=file_path.name,
                        issues=issues,
                        status="skipped_declined",
                        attempts=attempt,
                        error="",
                    )
                )
                break

            success, error = _execute_cleaning_code(code, cloned_path)
            if success:
                result.cleaned_files.append(
                    FileCleaningRecord(
                        file_name=file_path.name,
                        issues=issues,
                        status="cleaned",
                        attempts=attempt,
                    )
                )
                break

            previous_code, previous_error = code, error
            if attempt >= MAX_CLEAN_ATTEMPTS:
                result.skipped_files.append(
                    FileCleaningRecord(
                        file_name=file_path.name,
                        issues=issues,
                        status="skipped_failed",
                        attempts=attempt,
                        error=error,
                    )
                )

    return result
