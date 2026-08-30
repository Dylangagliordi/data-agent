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
clean_data.py and the ETL analyst's transform_load tool) and, once cleaning completes for
that file, load from folder_path/cleaned/ instead. A file that was skipped (declined
approval, or failed cleaning after exhausting retries) is reported clearly and excluded
from the load — it is never loaded raw once the rubric has flagged it, and never loaded
from a clone that didn't actually get cleaned.
"""

import csv
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

from utils.data_cleaning import clean_dataset

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

    cleaned_names = {rec.file_name for rec in cleaning_result.cleaned_files}
    skipped_names = {rec.file_name for rec in cleaning_result.skipped_files}

    load_plan = []  # list[(csv_path_to_actually_load, original_name)]
    for csv_path in csv_files:
        if csv_path.name in skipped_names:
            continue
        if csv_path.name in cleaned_names:
            load_plan.append((Path(cleaning_result.cleaned_dir) / csv_path.name, csv_path.name))
        else:
            load_plan.append((csv_path, csv_path.name))

    if skipped_names:
        print(
            f"\nSkipping load for {len(skipped_names)} file(s) that were not successfully "
            f"cleaned: {', '.join(sorted(skipped_names))}"
        )

    print(f"\nLoading {len(load_plan)} file(s) into Postgres...")
    conn = get_admin_connection()
    try:
        for load_path, original_name in load_plan:
            table_name, row_count = load_csv_to_table(conn, load_path)
            source_note = " (cleaned)" if original_name in cleaned_names else ""
            print(f"Loaded {original_name}{source_note} -> table '{table_name}' ({row_count} rows)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
