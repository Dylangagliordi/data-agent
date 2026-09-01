"""
Standalone data loader: loads all CSVs from a given folder into Postgres.

Usage:
    python utils/load_data.py data/olist

Notes:
- Uses the Postgres ADMIN connection only (never app_reader, which must stay read-only).
- Table name is derived from each CSV's filename (stem), lowercased.
- If a table with that name already exists, it is dropped first so a completely
  different dataset can be loaded cleanly without old tables lingering.
- This script is intentionally NOT part of the LangGraph graph — it's a manual,
  reusable utility you run whenever you want to (re)load a dataset folder.

Cleaning (Path A from the ETL analyst spec): before loading anything, this now calls
utils.data_cleaning.clean_dataset() on the folder. Files with nothing flagged load from
their original location exactly as before. Files needing cleaning go through the
approval-gated clone/generate/execute/retry process (same shared implementation used by
clean_data.py and the ETL analyst's transform_load tool).

Every file is now loaded regardless of cleaning outcome — a file that was declined at
the approval gate, or still has unresolved issues after cleaning exhausted its retries,
is loaded from whatever the best-available version is (the cleaned/ clone if cleaning
was ever attempted on it, the original raw file otherwise) rather than silently being
excluded from the database. Refusing to load used to be this script's way of protecting
against bad data; that job now belongs to _data_quality_status (see below), which lets
the SQL analyst honestly warn about a specific table's real, current quality state
instead of the loader deciding — invisibly, at load time — that a table simply
shouldn't exist for later querying.

Data-quality tracking: after loading each table, this writes/updates one row for it in
_data_quality_status (table_name text PK, last_loaded_at timestamp, status "pass"/
"warn"/"fail", issues_found jsonb, was_cleaned bool). status/issues_found reflect
whichever of the table's ORIGINAL check_rubric() issues are still actually unresolved
after clean_dataset() finished (see utils.data_cleaning.unresolved_issues_for_record),
classified with the existing _issue_severity() mapping — never a new/parallel severity
scheme. agents/sql_analyst.py's add_context reads this table to warn about tables that
were never checked or still have an unresolved fail-level issue.
"""

import csv
import os
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from utils.data_cleaning import (
    _issue_severity,
    clean_dataset,
    unresolved_issues_for_record,
)

load_dotenv(os.path.expanduser("~/.hermes/profiles/data-agent/.env"))


def get_admin_connection():
    """Connect as the Postgres admin/superuser — used only by this loader."""
    return psycopg2.connect(
        host=os.environ["PG_HOST"],
        port=os.environ["PG_PORT"],
        dbname=os.environ["PG_DATABASE"],
        user=os.environ["PG_ADMIN_USER"],
        # local trust auth for the admin user: no password needed/stored
    )


def ensure_data_quality_status_table(conn) -> None:
    """Create _data_quality_status if it doesn't already exist, and add any columns
    that were introduced after the initial schema. Table/column identifiers here can't
    be parameterized (DDL doesn't support placeholders for identifiers in any SQL
    dialect) but every identifier below is a fixed literal written in this source file,
    not runtime/user input, so this is not the kind of string-built SQL the project's
    "always use parameterized queries" rule targets — that rule is about values, and
    every value-position query elsewhere in this function/module already uses %s
    placeholders.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS _data_quality_status (
                table_name TEXT PRIMARY KEY,
                last_loaded_at TIMESTAMPTZ NOT NULL,
                status TEXT NOT NULL,
                issues_found JSONB NOT NULL,
                was_cleaned BOOLEAN NOT NULL
            );
            """
        )
        # source_folder added after initial schema — idempotent migration so older
        # installs gain the column on their next load_data.py run. Existing rows
        # will have source_folder = NULL (treated as "can't auto-redirect").
        cur.execute(
            """
            ALTER TABLE _data_quality_status
            ADD COLUMN IF NOT EXISTS source_folder TEXT;
            """
        )
    conn.commit()


def compute_quality_status(unresolved_issues: list) -> tuple:
    """Classify a file's still-unresolved issues (post clean_dataset()) into
    (status, issues_found_payload) using the existing _issue_severity() mapping —
    the one already defined in utils.data_cleaning, not a new/parallel scheme.

    status is "fail" if any unresolved issue is fail-level, "warn" if only warn-level
    issues remain, "pass" if none remain at all. issues_found_payload is the real list
    of {"issue": ..., "severity": ...} entries for those same unresolved issues (never
    just the summary status), for _data_quality_status.issues_found and for
    agents/sql_analyst.py's add_context to build a concrete WARNING message from.
    """
    issues_found_payload = [
        {"issue": issue, "severity": _issue_severity(issue)} for issue in unresolved_issues
    ]
    severities = {entry["severity"] for entry in issues_found_payload}
    if "fail" in severities:
        status = "fail"
    elif "warn" in severities:
        status = "warn"
    else:
        status = "pass"
    return status, issues_found_payload


def write_data_quality_status(
    conn,
    table_name: str,
    status: str,
    issues_found: list,
    was_cleaned: bool,
    source_folder: str | None = None,
) -> None:
    """Insert or update table_name's row in _data_quality_status. All values are bound
    as real placeholders — only last_loaded_at uses the server-side now() function.
    source_folder is the folder path load_data.py (or clean_and_reload) loaded this
    table from; NULL means unknown (table loaded before this column was added)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO _data_quality_status
                (table_name, last_loaded_at, status, issues_found, was_cleaned, source_folder)
            VALUES (%s, now(), %s, %s, %s, %s)
            ON CONFLICT (table_name) DO UPDATE SET
                last_loaded_at = EXCLUDED.last_loaded_at,
                status = EXCLUDED.status,
                issues_found = EXCLUDED.issues_found,
                was_cleaned = EXCLUDED.was_cleaned,
                source_folder = EXCLUDED.source_folder
            """,
            (table_name, status, psycopg2.extras.Json(issues_found), was_cleaned, source_folder),
        )
    conn.commit()


def infer_pg_type(values):
    """Very small heuristic type inferer for a column's sample of string values."""
    non_empty = [v for v in values if v not in (None, "")]
    if not non_empty:
        return "TEXT"

    def is_int(v):
        try:
            int(v)
            return True
        except ValueError:
            return False

    def is_float(v):
        try:
            float(v)
            return True
        except ValueError:
            return False

    if all(is_int(v) for v in non_empty):
        return "BIGINT"
    if all(is_float(v) for v in non_empty):
        return "DOUBLE PRECISION"
    return "TEXT"


def sanitize_identifier(name: str) -> str:
    """Lowercase, replace non-alnum with underscore, for use as a table/column name."""
    out = []
    for ch in name.strip().lower():
        out.append(ch if (ch.isalnum() or ch == "_") else "_")
    ident = "".join(out)
    if ident and ident[0].isdigit():
        ident = "_" + ident
    return ident or "col"


def load_csv_to_table(conn, csv_path: Path, sample_rows_for_typing: int = 500):
    table_name = sanitize_identifier(csv_path.stem)

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        columns = [sanitize_identifier(c) for c in header]

        # Sample rows to infer column types
        sample = []
        for i, row in enumerate(reader):
            sample.append(row)
            if i >= sample_rows_for_typing:
                break

        col_values = list(zip(*sample)) if sample else [[] for _ in columns]
        col_types = [infer_pg_type(vals) for vals in col_values] if sample else ["TEXT"] * len(columns)

    with conn.cursor() as cur:
        # Table names cannot be parameterized (DDL identifiers aren't valid placeholder
        # targets in any SQL dialect) — but table_name is derived from the local
        # filename and sanitized above, not user/network input, so building it into
        # the identifier here is safe as opposed to unsanitized SQL construction.
        cur.execute(f'DROP TABLE IF EXISTS "{table_name}" CASCADE;')

        col_defs = ", ".join(f'"{c}" {t}' for c, t in zip(columns, col_types))
        cur.execute(f'CREATE TABLE "{table_name}" ({col_defs});')

        # Re-open the file to stream all rows (avoid double-parsing / big memory use)
        with open(csv_path, newline="", encoding="utf-8") as f2:
            reader2 = csv.reader(f2)
            next(reader2)  # skip header
            placeholders = ", ".join(["%s"] * len(columns))
            quoted_columns = ", ".join(f'"{c}"' for c in columns)
            insert_sql = (
                f'INSERT INTO "{table_name}" ({quoted_columns}) '
                f"VALUES ({placeholders})"
            )
            batch = []
            batch_size = 1000
            row_count = 0
            for row in reader2:
                cleaned = [None if v == "" else v for v in row]
                batch.append(cleaned)
                row_count += 1
                if len(batch) >= batch_size:
                    cur.executemany(insert_sql, batch)
                    batch = []
            if batch:
                cur.executemany(insert_sql, batch)

    conn.commit()
    return table_name, row_count


def main() -> None:
    if len(sys.argv) != 2:
        print(
            "ERROR: you must specify a folder path to load, e.g.:\n"
            "    python utils/load_data.py data/olist",
            file=sys.stderr,
        )
        sys.exit(1)

    folder = Path(sys.argv[1])
    if not folder.is_dir():
        print(f"ERROR: folder not found: {folder}", file=sys.stderr)
        sys.exit(1)

    csv_files = sorted(folder.glob("*.csv"))
    if not csv_files:
        print(f"ERROR: no CSV files found in {folder}", file=sys.stderr)
        sys.exit(1)

    print(f"Checking {folder} against the cleaning rubric before loading...")
    cleaning_result = clean_dataset(folder)
    print(cleaning_result.summary())

    # Every file loads regardless of cleaning outcome now — see module docstring.
    # cleaned_names: cleaning fully resolved every original issue -> load from cleaned/.
    # attempted_names: cleaning was attempted but declined or left something unresolved
    # -> still load from cleaned/ (the clone always exists once issues were found, even
    # if some/all of the generated fixes were declined or didn't stick).
    records_by_name = {rec.file_name: rec for rec in cleaning_result.cleaned_files}
    records_by_name.update({rec.file_name: rec for rec in cleaning_result.skipped_files})
    cleaned_names = {rec.file_name for rec in cleaning_result.cleaned_files}
    attempted_names = set(records_by_name.keys())

    load_plan = []  # list[(csv_path_to_actually_load, original_name)]
    for csv_path in csv_files:
        if csv_path.name in attempted_names:
            load_plan.append((Path(cleaning_result.cleaned_dir) / csv_path.name, csv_path.name))
        else:
            load_plan.append((csv_path, csv_path.name))

    print(f"\nLoading {len(load_plan)} file(s) into Postgres...")
    conn = get_admin_connection()
    try:
        ensure_data_quality_status_table(conn)
        for load_path, original_name in load_plan:
            table_name, row_count = load_csv_to_table(conn, load_path)
            source_note = " (cleaned)" if original_name in cleaned_names else ""
            print(f"Loaded {original_name}{source_note} -> table '{table_name}' ({row_count} rows)")

            rec = records_by_name.get(original_name)
            if rec is None:
                # Untouched: check_rubric() found nothing at all for this file.
                unresolved_issues = []
                was_cleaned = False
            else:
                unresolved_issues = unresolved_issues_for_record(rec)
                was_cleaned = True
            status, issues_found = compute_quality_status(unresolved_issues)
            write_data_quality_status(
                conn, table_name, status, issues_found, was_cleaned,
                source_folder=str(folder),
            )
            print(f"  -> data quality status: {status} ({len(issues_found)} unresolved issue(s))")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
