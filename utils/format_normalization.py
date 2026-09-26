"""Format Normalization (Spec 10, Part 1): convert JSON/Excel/HTML files into
CSV before utils.data_cleaning.clean_dataset() ever sees them.

clean_dataset() only ever globs a folder for `*.csv` (utils/data_cleaning.py:
`csv_files = sorted(p for p in folder.glob("*.csv") if p.is_file())`) — a
JSON, Excel, or HTML file sitting in that same folder is completely invisible
to the cleaning pipeline no matter how it got there (a manual copy, or a
source that only offers a non-CSV format). This is a pre-pass that runs
BEFORE clean_dataset(), never a change to clean_dataset() itself — zero risk
to its own extensive, already-passing test suite.

Public interface:
    normalize_to_csv(file_path) -> Path          (single file)
    normalize_folder_to_csv(folder_path) -> dict  (every supported file in a folder)
"""

import io
from pathlib import Path

import pandas as pd

SUPPORTED_EXTENSIONS = {".json", ".xlsx", ".xls", ".html", ".htm"}


def largest_table(tables: list) -> pd.DataFrame:
    """pd.read_html returns every <table> element found in a document — a
    real page often has several (navigation, footer, ads) alongside the one
    real data table. Picks the largest by cell count (rows * columns): a
    real, explicit, stated rule, not a silent default to "whichever table
    happens to appear first," which is frequently not the meaningful one.
    Shared with agents/etl_analyst.py:scrape_load so both the "I already
    downloaded an HTML file" and "scrape a live page" paths pick a table the
    same, documented way.
    """
    return max(tables, key=lambda df: df.shape[0] * df.shape[1])


def normalize_to_csv(file_path) -> Path:
    """Converts file_path into a sibling CSV (same stem, .csv extension) and
    returns its path. A file that's already .csv is returned UNCHANGED — no
    conversion, no new file written — so a caller can call this
    unconditionally on every file in a folder without its own type check.

    Raises ValueError for an unsupported extension, and lets whatever pandas
    raises for a file that fails to parse propagate — never silently writes
    an empty or garbage CSV.
    """
    path = Path(file_path)
    suffix = path.suffix.lower()

    if suffix == ".csv":
        return path

    if suffix == ".json":
        df = pd.read_json(path)
    elif suffix in (".xlsx", ".xls"):
        df = pd.read_excel(path)
    elif suffix in (".html", ".htm"):
        tables = pd.read_html(path)
        if not tables:
            raise ValueError(f"no <table> elements found in {path}")
        df = largest_table(tables)
    else:
        raise ValueError(
            f"unsupported file extension {suffix!r} for {path} — format "
            f"normalization only handles {sorted(SUPPORTED_EXTENSIONS)}"
        )

    out_path = path.with_suffix(".csv")
    df.to_csv(out_path, index=False)
    return out_path


def normalize_folder_to_csv(folder_path) -> dict:
    """Converts every supported non-CSV file at the top level of folder_path
    into a sibling CSV, skipping a file whose .csv counterpart already
    exists (never overwrites an existing, possibly-already-cleaned file).

    Returns {"written": [Path, ...], "errors": ["<filename>: <error>", ...]}.
    A file that fails to convert is recorded in "errors" rather than aborting
    the whole folder — one bad file must never block every other file in it.
    """
    folder = Path(folder_path)
    written = []
    errors = []
    for file_path in sorted(folder.iterdir()):
        if not file_path.is_file() or file_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        csv_path = file_path.with_suffix(".csv")
        if csv_path.exists():
            continue
        try:
            written.append(normalize_to_csv(file_path))
        except Exception as e:
            errors.append(f"{file_path.name}: {type(e).__name__}: {e}")
    return {"written": written, "errors": errors}
