"""
Spec 1, Part 7 — Feature engineering: judgment-call DERIVED columns, kept
structurally separate from clean_dataset() (Phase 1's objectively-wrong-data
fixes) — same reasoning as Spec 3's range decomposition and this project's
missing-value-strategy fixed-rule decision. A derived feature is a modeling/
analysis CHOICE, not a correctness fix; there's no single "right" derivation,
so it goes through Transformation Options (utils/transformation_options.py),
never applied automatically.

Five candidate kinds, each independently detected and independently offered
(see detect_feature_derivation_candidates):
- "job_title_categorization" -> one new column, "Updated Job Title".
- "company_age" -> one new column, "company_age". Missing-Founded handling
  (null vs. "founded this year" = age 0) is its own explicit sub-choice,
  carried on the candidate/spec as `missing_founded_choice` — the "age 0"
  option must be presented as a deliberate assumption, not an observed fact.
- "same_state_flag" -> one new column, "same_state".
- "skill_keywords" -> several new boolean columns, one per tracked skill
  keyword (Python/Excel/Hadoop/Spark/AWS/Tableau/Big Data).
- "seniority_flag" -> one new column, "seniority_level". This dataset has no
  distinct "Job Type" column (the spec's own example name) — the real,
  available signal is seniority language embedded in "Job Title" itself
  (e.g. "Sr Data Scientist", "Data Scientist II"), so detection targets that
  column instead; see _detect_seniority_flag's docstring.

Every kind reuses the exact same opt-in generate -> approve -> execute ->
re-check -> retry cycle as Spec 3's decompose_range_column (same
_request_approval/_execute_cleaning_code, no weaker approval path for a
derived feature than for a real data-quality fix), and the same first-class
decline discipline (Spec 2.1's lesson, NOT retrofitted here): a legitimate
"this derivation doesn't apply" response is a NO_DERIVATION sentinel, checked
BEFORE any approval prompt or retry pressure.

Derived columns are marked "derived, not source" via
utils.load_data.mark_derived_columns / get_derived_columns (a small parallel
table, _derived_columns) so generate_sql/disclosure logic can tell a computed
value apart from an observed one — see that module for the table shape.
"""

import re
from dataclasses import dataclass, field

FEATURE_DERIVATION_KINDS = (
    "job_title_categorization",
    "company_age",
    "same_state_flag",
    "skill_keywords",
    "seniority_flag",
)

# The exact new column name(s) each kind is allowed to add — the real,
# deterministic post-fix shape check (_derivation_shape_ok) enforces this
# precisely, unlike range decomposition's looser "looks numeric" check, since
# these column names are fixed by this spec rather than user-chosen.
FEATURE_EXPECTED_NEW_COLUMNS = {
    "job_title_categorization": ["Updated Job Title"],
    "company_age": ["company_age"],
    "same_state_flag": ["same_state"],
    "skill_keywords": [
        "has_python", "has_excel", "has_hadoop", "has_spark",
        "has_aws", "has_tableau", "has_big_data",
    ],
    "seniority_flag": ["seniority_level"],
}

_JOB_TITLE_NAME_HINTS = ("job title", "title", "position", "role")
_FOUNDED_NAME_HINTS = ("founded", "founding year", "year founded")
_HEADQUARTERS_NAME_HINTS = ("headquarters", "hq")
_LOCATION_NAME_HINTS = ("location", "city")
_DESCRIPTION_NAME_HINTS = ("description", "summary", "responsibilities")

SKILL_KEYWORDS = ("python", "excel", "hadoop", "spark", "aws", "tableau", "big data")


def _find_column_by_hints(df, hints, exclude_hints=()) -> "str | None":
    """First real column whose lowercased name contains any of `hints` and
    none of `exclude_hints` — deterministic, name-based detection (same
    discipline as _detect_range_columns: a scan, never modifies df)."""
    for col in df.columns:
        col_lower = col.lower()
        if any(hint in col_lower for hint in exclude_hints):
            continue
        if any(hint in col_lower for hint in hints):
            return col
    return None


def _detect_job_title_categorization(df) -> "dict | None":
    col = _find_column_by_hints(df, _JOB_TITLE_NAME_HINTS)
    if col is None:
        return None
    sample = df[col].dropna().astype(str)
    if sample.empty:
        return None
    return {
        "columns": [col],
        "description": (
            f"Column '{col}' holds {sample.nunique()} distinct free-text job titles "
            f"(e.g. {sample.iloc[0]!r}) — could be categorized into a small set of "
            "standardized role categories (e.g. Data Scientist, Data Engineer, Data "
            "Analyst, ML Engineer, Manager, Other)."
        ),
    }


def _detect_company_age(df) -> "dict | None":
    col = _find_column_by_hints(df, _FOUNDED_NAME_HINTS)
    if col is None:
        return None
    numeric = __import__("pandas").to_numeric(df[col], errors="coerce").dropna()
    if numeric.empty:
        return None
    missing_frac = df[col].isna().mean() + (df[col].astype(str).str.strip() == "-1").mean()
    return {
        "columns": [col],
        "description": (
            f"Column '{col}' holds a founding year for {len(numeric)} real rows "
            f"(e.g. {int(numeric.iloc[0])}) — could derive 'company_age' "
            f"(current year minus founding year). {missing_frac:.0%} of rows have no "
            "usable founding year and need an explicit missing-value choice."
        ),
    }


def _detect_same_state_flag(df) -> "dict | None":
    hq_col = _find_column_by_hints(df, _HEADQUARTERS_NAME_HINTS)
    loc_col = _find_column_by_hints(df, _LOCATION_NAME_HINTS, exclude_hints=_HEADQUARTERS_NAME_HINTS)
    if hq_col is None or loc_col is None or hq_col == loc_col:
        return None
    return {
        "columns": [loc_col, hq_col],
        "description": (
            f"Columns '{loc_col}' and '{hq_col}' both hold 'City, State'-shaped "
            "location text — could derive a 'same_state' flag comparing the job's "
            "location state against the company's headquarters state."
        ),
    }


def _detect_skill_keywords(df) -> "dict | None":
    col = _find_column_by_hints(df, _DESCRIPTION_NAME_HINTS)
    if col is None:
        return None
    text = df[col].dropna().astype(str).str.lower()
    if text.empty:
        return None
    found = [kw for kw in SKILL_KEYWORDS if text.str.contains(re.escape(kw), regex=True).any()]
    if not found:
        return None
    return {
        "columns": [col],
        "description": (
            f"Column '{col}' contains real mentions of {len(found)} tracked skill "
            f"keyword(s) ({', '.join(found)}) — could derive one boolean flag column "
            "per keyword."
        ),
    }


_SENIOR_KEYWORDS = ("senior", "sr.", "sr ", "lead", "principal", "staff", "ii", "iii")
_JUNIOR_KEYWORDS = ("junior", "jr.", "jr ", "entry", "associate", " i ", "intern")


def _detect_seniority_flag(df) -> "dict | None":
    """This dataset has no distinct "Job Type" column (the spec's own example
    name for this candidate) — the real, available seniority signal is
    language embedded in the job-title column itself (e.g. "Sr Data
    Scientist", "Data Scientist II"), so detection targets that instead."""
    col = _find_column_by_hints(df, _JOB_TITLE_NAME_HINTS)
    if col is None:
        return None
    titles = df[col].dropna().astype(str).str.lower()
    if titles.empty:
        return None
    has_signal = titles.apply(
        lambda t: any(k in t for k in _SENIOR_KEYWORDS) or any(k in t for k in _JUNIOR_KEYWORDS)
    )
    if not has_signal.any():
        return None
    return {
        "columns": [col],
        "description": (
            f"Column '{col}' has {int(has_signal.sum())} value(s) ({has_signal.mean():.0%}) "
            "containing seniority language (e.g. 'senior', 'sr', 'ii', 'junior', 'entry') — "
            "could derive a 'seniority_level' flag (Senior / Junior / Not specified)."
        ),
    }


_DETECTORS = {
    "job_title_categorization": _detect_job_title_categorization,
    "company_age": _detect_company_age,
    "same_state_flag": _detect_same_state_flag,
    "skill_keywords": _detect_skill_keywords,
    "seniority_flag": _detect_seniority_flag,
}


def detect_feature_derivation_candidates(df) -> list:
    """Runs every kind-specific detector above (deterministic, no LLM — same
    discipline as _detect_range_columns) and returns a flat list of
    {"kind", "columns", "description"} dicts, one per kind that found a real
    signal in `df`. Never modifies df. Consumed by
    utils.transformation_options.detect_transformation_candidates, which
    wraps each into the shared TransformationCandidate shape."""
    found = []
    for kind, detector in _DETECTORS.items():
        result = detector(df)
        if result is not None:
            found.append({"kind": kind, **result})
    return found


# ---------------------------------------------------------------------------
# Opt-in application: generate -> approve -> execute -> re-check -> retry,
# mirroring utils.data_cleaning.decompose_range_column exactly.
# ---------------------------------------------------------------------------

FEATURE_DERIVATION_SYSTEM_PROMPTS = {
    "job_title_categorization": """You are deriving a new column, "Updated Job Title", from
the real job-title column named below, by categorizing each free-text title into a SMALL,
consistent set of role categories based on the real sample values shown (e.g. "Data
Scientist", "Data Engineer", "Data Analyst", "Machine Learning Engineer", "Manager",
"Other" — adjust the exact category set to what the real samples actually show; do not
invent categories with zero real support).

IMPORTANT — check this first: if the named column does not actually look like job titles
worth categorizing (e.g. it's already a small fixed set of categories, or the values are
not job titles at all), output exactly this and nothing else:

# NO_DERIVATION: <one-sentence reason>

Otherwise, follow these rules:
- Add EXACTLY ONE new column, "Updated Job Title".
- The ORIGINAL column MUST be preserved completely unchanged.
- You MUST NOT touch any other column in the file.
- Every real row must get a real category — do not leave "Updated Job Title" null unless
  the source value itself is null/missing.
- Output ONLY raw Python code — no explanation, no markdown fences — UNLESS declining.
- The script must read the CSV at the exact path given, add the new column, and write the
  result back to that same path (overwrite in place).
""",
    "company_age": """You are deriving a new column, "company_age", from the real founding-
year column named below: company_age = (current calendar year) - (founding year).

IMPORTANT — check this first: if the named column does not actually hold real founding
years worth deriving an age from, output exactly this and nothing else:

# NO_DERIVATION: <one-sentence reason>

Otherwise, follow these rules:
- Add EXACTLY ONE new column, "company_age" (a number).
- The ORIGINAL column MUST be preserved completely unchanged.
- You MUST NOT touch any other column in the file.
- Missing-value handling is a DELIBERATE, EXPLICIT choice, stated below — follow it exactly,
  and add a one-line code comment naming which choice you followed:
  {missing_founded_instruction}
- Output ONLY raw Python code — no explanation, no markdown fences — UNLESS declining.
- The script must read the CSV at the exact path given, add the new column, and write the
  result back to that same path (overwrite in place).
""",
    "same_state_flag": """You are deriving a new column, "same_state", comparing the STATE
parsed from the job-location column against the STATE parsed from the headquarters column
named below (both are real "City, State"-shaped text, e.g. "Boston, MA" or an international
"City, Country" value with no US state at all).

IMPORTANT — check this first: if the named columns do not actually hold comparable
location text, output exactly this and nothing else:

# NO_DERIVATION: <one-sentence reason>

Otherwise, follow these rules:
- Add EXACTLY ONE new column, "same_state" (boolean: True/False).
- A row where either side has no parseable US state (e.g. an international headquarters,
  or a missing/placeholder value) must get "same_state" = null/NaN, never a guessed
  True/False.
- The ORIGINAL columns MUST be preserved completely unchanged.
- You MUST NOT touch any other column in the file.
- Output ONLY raw Python code — no explanation, no markdown fences — UNLESS declining.
- The script must read the CSV at the exact path given, add the new column, and write the
  result back to that same path (overwrite in place).
""",
    "skill_keywords": """You are deriving several new boolean flag columns from the real
free-text column named below, one per tracked skill keyword found in it: python, excel,
hadoop, spark, aws, tableau, big data.

IMPORTANT — check this first: if the named column does not actually contain any of these
keywords for a meaningful share of rows, output exactly this and nothing else:

# NO_DERIVATION: <one-sentence reason>

Otherwise, follow these rules:
- Add EXACTLY these new boolean columns, one per keyword actually worth flagging:
  has_python, has_excel, has_hadoop, has_spark, has_aws, has_tableau, has_big_data.
- Each is True if that keyword appears (case-insensitive, substring match) in the row's
  text, else False. A null/missing source value gets False for every flag (not null) —
  "no description available" is not evidence of a skill's absence, but flags are meant to
  be usable directly in a WHERE/GROUP BY without extra null-handling; document this
  convention as a one-line comment.
- The ORIGINAL column MUST be preserved completely unchanged.
- You MUST NOT touch any other column in the file.
- Output ONLY raw Python code — no explanation, no markdown fences — UNLESS declining.
- The script must read the CSV at the exact path given, add the new columns, and write the
  result back to that same path (overwrite in place).
""",
    "seniority_flag": """You are deriving a new column, "seniority_level", from the real
job-title column named below, based on seniority language actually present in the titles
(e.g. "senior", "sr", "ii"/"iii", "lead", "principal", "staff" -> "Senior"; "junior", "jr",
"entry", "associate", "intern" -> "Junior"; anything else -> "Not specified").

IMPORTANT — check this first: if the named column does not actually contain real
seniority language worth flagging, output exactly this and nothing else:

# NO_DERIVATION: <one-sentence reason>

Otherwise, follow these rules:
- Add EXACTLY ONE new column, "seniority_level", with values from EXACTLY the set
  {{"Senior", "Junior", "Not specified"}}.
- The ORIGINAL column MUST be preserved completely unchanged.
- You MUST NOT touch any other column in the file.
- Output ONLY raw Python code — no explanation, no markdown fences — UNLESS declining.
- The script must read the CSV at the exact path given, add the new column, and write the
  result back to that same path (overwrite in place).
""",
}

_MISSING_FOUNDED_INSTRUCTIONS = {
    "null": (
        "leave company_age NULL/NaN for any row with a missing, non-numeric, or "
        "placeholder (e.g. '-1') founding year — do not guess an age."
    ),
    "age_zero": (
        "for any row with a missing, non-numeric, or placeholder (e.g. '-1') founding "
        "year, treat it as founded THIS YEAR (company_age = 0) — this is a DELIBERATE "
        "ASSUMPTION, not an observed fact, and must be stated as such in the code comment."
    ),
}

_NO_DERIVATION_RE = re.compile(r"^#\s*NO_DERIVATION:\s*(.+)$", re.IGNORECASE)


def _extract_no_derivation_reason(code: str) -> "str | None":
    """Same discipline as _extract_no_split_reason / _extract_no_decomposition_reason
    — returns the decline reason if `code` is exactly a NO_DERIVATION sentinel
    response, else None."""
    stripped = (code or "").strip()
    match = _NO_DERIVATION_RE.match(stripped)
    if match and "\n" not in stripped:
        return match.group(1).strip()
    return None


def _generate_feature_derivation_code(
    file_path, kind: str, columns: list, llm, missing_founded_choice: str = "null",
    previous_code: str = "", previous_error: str = "",
) -> str:
    """Mirrors utils.data_cleaning._generate_range_decomposition_code's shape
    (_describe_file_for_prompt, previous_code/previous_error on retries,
    _strip_code_formatting) but uses the kind-specific
    FEATURE_DERIVATION_SYSTEM_PROMPTS entry."""
    from utils.data_cleaning import _describe_file_for_prompt, _read_csv_robust, _strip_code_formatting

    df = _read_csv_robust(file_path)
    file_context = _describe_file_for_prompt(file_path, df=df)
    system_prompt = FEATURE_DERIVATION_SYSTEM_PROMPTS[kind]
    if kind == "company_age":
        system_prompt = system_prompt.format(
            missing_founded_instruction=_MISSING_FOUNDED_INSTRUCTIONS[missing_founded_choice]
        )

    human_content = (
        f"File to clean (read and overwrite this exact path): {file_path}\n\n"
        f"{file_context}\n\n"
        f"Real column(s) to derive from: {', '.join(columns)}"
    )
    if previous_error:
        human_content += (
            f"\n\nA PREVIOUS ATTEMPT was rejected:\n{previous_code}\n\nReason: {previous_error}\n"
            "Fix the issue and try again."
        )

    response = llm.invoke([("system", system_prompt), ("human", human_content)])
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


def _derivation_shape_ok(kind: str, original_columns: list, current_columns: list) -> tuple:
    """Real post-fix verification (same role as _range_decomposition_shape_ok/
    _composite_split_shape_ok): the original columns must be PRESERVED, and
    the new columns present must be EXACTLY FEATURE_EXPECTED_NEW_COLUMNS[kind]
    — a fixed, known set (unlike range decomposition's looser "looks numeric"
    check), since these names are defined by this spec, not user-chosen.
    Returns (ok, reason)."""
    missing_originals = [c for c in original_columns if c not in current_columns]
    if missing_originals:
        return False, f"Original column(s) {missing_originals} were removed or renamed — must be preserved."

    new_columns = [c for c in current_columns if c not in original_columns]
    expected = FEATURE_EXPECTED_NEW_COLUMNS[kind]
    if set(new_columns) != set(expected):
        return False, (
            f"Expected exactly the new column(s) {expected} for '{kind}', found {new_columns}."
        )
    return True, ""


def derive_features(file_path, feature_spec: dict, llm=None) -> dict:
    """THE OPT-IN FIX — never called automatically by clean_dataset() or by
    detect_transformation_candidates(). A separate caller (Transformation
    Options' present_transformation_options, or a future orchestrator)
    decides whether/when to invoke this for a chosen candidate.

    feature_spec: {"kind": one of FEATURE_DERIVATION_KINDS, "columns": [...],
    "missing_founded_choice": "null" | "age_zero" — only meaningful for
    "company_age", defaults to "null"}.

    Own generate -> approve -> execute -> re-check cycle, entirely separate
    from clean_dataset()'s fail/warn loop, reusing the exact same
    _request_approval/_execute_cleaning_code (no new or weaker approval path
    for a derived feature than for a real data-quality fix). Checks
    _extract_no_derivation_reason immediately after every generation, BEFORE
    any approval prompt or execution — a legitimate decline returns
    "declined_false_positive" right away, never fed into the retry loop
    (Spec 2.1's lesson applied from the start, not retrofitted).

    Returns {"kind", "status", "attempts", "error", "new_columns",
    "generated_code"}; status uses the same vocabulary as decompose_range_column's
    ("resolved" | "skipped_declined" | "skipped_failed" | "skipped_incomplete"
    | "declined_false_positive").
    """
    from utils.data_cleaning import (
        MAX_CLEAN_ATTEMPTS,
        _execute_cleaning_code,
        _read_csv_robust,
        _request_approval,
    )
    from utils.llm_pick import pick_llm

    kind = feature_spec["kind"]
    columns = feature_spec["columns"]
    missing_founded_choice = feature_spec.get("missing_founded_choice", "null")
    resolved_llm = llm if llm is not None else pick_llm("high")

    original_columns = list(_read_csv_robust(file_path).columns)

    previous_code = ""
    previous_error = ""
    attempt = 0
    while attempt < MAX_CLEAN_ATTEMPTS:
        attempt += 1
        code = _generate_feature_derivation_code(
            file_path, kind, columns, resolved_llm, missing_founded_choice,
            previous_code, previous_error,
        )

        no_derivation_reason = _extract_no_derivation_reason(code)
        if no_derivation_reason is not None:
            return {
                "kind": kind, "status": "declined_false_positive", "attempts": attempt,
                "error": no_derivation_reason, "new_columns": [], "generated_code": "",
            }

        approved = _request_approval(code, file_path)
        if not approved:
            return {
                "kind": kind, "status": "skipped_declined", "attempts": attempt,
                "error": "", "new_columns": [], "generated_code": "",
            }

        success, error = _execute_cleaning_code(code, file_path)
        if not success:
            previous_code, previous_error = code, error
            if attempt >= MAX_CLEAN_ATTEMPTS:
                return {
                    "kind": kind, "status": "skipped_failed", "attempts": attempt,
                    "error": error, "new_columns": [], "generated_code": code,
                }
            continue

        current_columns = list(_read_csv_robust(file_path).columns)
        shape_ok, shape_reason = _derivation_shape_ok(kind, original_columns, current_columns)
        if shape_ok:
            new_columns = [c for c in current_columns if c not in original_columns]
            return {
                "kind": kind, "status": "resolved", "attempts": attempt,
                "error": "", "new_columns": new_columns, "generated_code": code,
            }

        previous_code, previous_error = code, shape_reason
        if attempt >= MAX_CLEAN_ATTEMPTS:
            return {
                "kind": kind, "status": "skipped_incomplete", "attempts": attempt,
                "error": shape_reason, "new_columns": [], "generated_code": code,
            }

    return {
        "kind": kind, "status": "skipped_failed", "attempts": attempt,
        "error": "exhausted attempts", "new_columns": [], "generated_code": "",
    }
