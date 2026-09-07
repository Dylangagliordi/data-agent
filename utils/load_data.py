"""
Standalone data loader: loads all CSVs from a given folder into Postgres.

Usage:
    python utils/load_data.py data/olist

Notes:
- Uses the Postgres ADMIN connection only (never app_reader, which must stay read-only).
- Table name is derived from each CSV's filename (stem), lowercased.
- Loading is ATOMIC (see load_csv_to_table): new data is built in a staging table
  first, and only swapped into place — via a single-transaction ALTER TABLE ...
  RENAME — once that staging load fully succeeds. An existing table of the same name
  is never dropped outright; it's renamed to <table>_previous (one generation of
  rollback — see rollback_table) rather than deleted, so a completely different
  dataset can still be loaded cleanly without old tables lingering under their
  original name, while a bad reload remains recoverable.
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
"warn"/"fail", issues_found jsonb, was_cleaned bool, source_folder text,
source_checksum text). status/issues_found reflect whichever of the table's ORIGINAL
check_rubric() issues are still actually unresolved after clean_dataset() finished
(see utils.data_cleaning.unresolved_issues_for_record), classified with the existing
_issue_severity() mapping — never a new/parallel severity scheme. agents/sql_analyst.py's
add_context reads this table to warn about tables that were never checked or still
have an unresolved fail-level issue. source_checksum is the raw source file's real
SHA-256 at the time it was processed (see compute_file_checksum /
check_source_freshness) — a source-drift detection signal, not a load gate.
"""

import csv
import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

from utils.data_cleaning import (
    _issue_severity,
    _read_csv_robust,
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


_ID_COLUMN_RE = re.compile(r"_id$")


def _is_id_like_column(col: str) -> bool:
    """True for 'id' or any column ending in '_id' — same logic as in sql_analyst.py."""
    return col == "id" or bool(_ID_COLUMN_RE.search(col))


def ensure_fanout_status_table(conn) -> None:
    """Create _fanout_status if it doesn't already exist, and grant SELECT to app_reader.

    Table identifiers below are fixed literals from this source file, not runtime/user
    input — same rationale as ensure_data_quality_status_table (DDL identifiers can't
    use %s placeholders in any SQL dialect).
    """
    app_reader = os.environ.get("PG_APP_READER_USER", "app_reader")
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS _fanout_status (
                table_name   TEXT NOT NULL,
                column_name  TEXT NOT NULL,
                is_likely_fk BOOLEAN NOT NULL,
                has_fanout   BOOLEAN NOT NULL,
                source       TEXT NOT NULL,
                checked_at   TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (table_name, column_name)
            );
            """
        )
        cur.execute(f'GRANT SELECT ON _fanout_status TO "{app_reader}";')
    conn.commit()


def compute_and_write_fanout_status(conn, table_name: str) -> None:
    """Compute fan-out metadata for id-like columns in table_name and persist to
    _fanout_status. Called once after each table load.

    Three-tier logic (checked in order):
    1. Declared PRIMARY KEY: not a FK, no fan-out. source='declared_pk'
    2. Declared FOREIGN KEY: is a FK; cardinality determines has_fanout. source='declared_fk'
    3. Id-like column with no declared constraint: cardinality heuristic.
       - Unique within own table -> inferred PK (is_likely_fk=False). source='cardinality_heuristic'
       - Non-unique -> inferred FK with fan-out (is_likely_fk=True). source='cardinality_heuristic'

    Replaces any existing _fanout_status rows for this table so a reload always reflects
    current data, not stale pre-reload cardinality.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = tc.constraint_name
               AND kcu.table_schema   = tc.table_schema
               AND kcu.table_name     = tc.table_name
            WHERE tc.table_schema = 'public'
              AND tc.table_name   = %s
              AND tc.constraint_type = 'PRIMARY KEY'
            """,
            (table_name,),
        )
        declared_pks = {row[0] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON kcu.constraint_name = tc.constraint_name
               AND kcu.table_schema   = tc.table_schema
               AND kcu.table_name     = tc.table_name
            WHERE tc.table_schema = 'public'
              AND tc.table_name   = %s
              AND tc.constraint_type = 'FOREIGN KEY'
            """,
            (table_name,),
        )
        declared_fks = {row[0] for row in cur.fetchall()}

        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public'
              AND table_name   = %s
            ORDER BY ordinal_position
            """,
            (table_name,),
        )
        all_columns = [row[0] for row in cur.fetchall()]

    covered = declared_pks | declared_fks
    id_like_uncovered = [c for c in all_columns if _is_id_like_column(c) and c not in covered]

    def cardinality(col):
        with conn.cursor() as cur:
            cur.execute(
                f'SELECT COUNT(*), COUNT(DISTINCT "{col}") FROM "{table_name}"'
            )
            return cur.fetchone()

    rows_to_insert = []

    for col in declared_pks:
        rows_to_insert.append((table_name, col, False, False, "declared_pk"))

    for col in declared_fks:
        if col in declared_pks:
            continue
        total, distinct = cardinality(col)
        rows_to_insert.append((table_name, col, True, total > distinct, "declared_fk"))

    for col in id_like_uncovered:
        total, distinct = cardinality(col)
        if total == distinct:
            rows_to_insert.append((table_name, col, False, False, "cardinality_heuristic"))
        else:
            rows_to_insert.append((table_name, col, True, True, "cardinality_heuristic"))

    with conn.cursor() as cur:
        cur.execute("DELETE FROM _fanout_status WHERE table_name = %s", (table_name,))
        if rows_to_insert:
            cur.executemany(
                """
                INSERT INTO _fanout_status
                    (table_name, column_name, is_likely_fk, has_fanout, source, checked_at)
                VALUES (%s, %s, %s, %s, %s, now())
                """,
                rows_to_insert,
            )
    conn.commit()


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
        # source_checksum added for source-freshness tracking (architecture review
        # point #20) — same idempotent-migration pattern as source_folder above.
        # Existing rows will have source_checksum = NULL (treated as "no baseline
        # yet"; see check_source_freshness).
        cur.execute(
            """
            ALTER TABLE _data_quality_status
            ADD COLUMN IF NOT EXISTS source_checksum TEXT;
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
    source_checksum: str | None = None,
) -> None:
    """Insert or update table_name's row in _data_quality_status. All values are bound
    as real placeholders — only last_loaded_at uses the server-side now() function.
    source_folder is the folder path load_data.py (or clean_and_reload) loaded this
    table from; NULL means unknown (table loaded before this column was added).
    source_checksum is the SHA-256 hex digest of the raw source file's bytes at the
    time it was processed (see compute_file_checksum / check_source_freshness); NULL
    means no baseline recorded yet (table loaded before checksum tracking existed, or
    caller didn't pass one)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO _data_quality_status
                (table_name, last_loaded_at, status, issues_found, was_cleaned,
                 source_folder, source_checksum)
            VALUES (%s, now(), %s, %s, %s, %s, %s)
            ON CONFLICT (table_name) DO UPDATE SET
                last_loaded_at = EXCLUDED.last_loaded_at,
                status = EXCLUDED.status,
                issues_found = EXCLUDED.issues_found,
                was_cleaned = EXCLUDED.was_cleaned,
                source_folder = EXCLUDED.source_folder,
                source_checksum = EXCLUDED.source_checksum
            """,
            (
                table_name, status, psycopg2.extras.Json(issues_found), was_cleaned,
                source_folder, source_checksum,
            ),
        )
    conn.commit()


def ensure_transformation_candidates_table(conn) -> None:
    """Create _transformation_candidates if it doesn't already exist, and grant
    SELECT to app_reader (read at question time via the read-only connection —
    see utils.transformation_options.surface_relevant_transformations).

    One row per table_name, holding the FULL current list of
    TransformationCandidate dicts (Spec 1, Part 0) as JSONB — replaced wholesale
    on every genuine reload/re-clean of that table (never merged/appended), so a
    stale candidate that no longer applies to the new data can never linger.
    """
    app_reader = os.environ.get("PG_APP_READER_USER", "app_reader")
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS _transformation_candidates (
                table_name  TEXT PRIMARY KEY,
                detected_at TIMESTAMPTZ NOT NULL,
                candidates  JSONB NOT NULL
            );
            """
        )
        cur.execute(f'GRANT SELECT ON _transformation_candidates TO "{app_reader}";')
    conn.commit()


def write_transformation_candidates(conn, table_name: str, candidates: list) -> None:
    """Replace table_name's stored candidate list wholesale (Spec 1, Part 0 —
    detection runs once per table and its result is persisted, never
    re-detected live at question time). `candidates` is a list of
    TransformationCandidate (or plain dict) objects."""
    payload = [c.to_dict() if hasattr(c, "to_dict") else c for c in candidates]
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO _transformation_candidates (table_name, detected_at, candidates)
            VALUES (%s, now(), %s)
            ON CONFLICT (table_name) DO UPDATE SET
                detected_at = EXCLUDED.detected_at,
                candidates = EXCLUDED.candidates
            """,
            (table_name, psycopg2.extras.Json(payload)),
        )
    conn.commit()


def read_transformation_candidates(conn, table_name: str) -> list:
    """Return table_name's stored candidates as a list of TransformationCandidate
    objects (empty list if none have ever been detected for this table)."""
    from utils.transformation_options import TransformationCandidate

    with conn.cursor() as cur:
        cur.execute(
            "SELECT candidates FROM _transformation_candidates WHERE table_name = %s",
            (table_name,),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return []
    return [TransformationCandidate.from_dict(d) for d in row[0]]


def ensure_transformation_decisions_table(conn) -> None:
    """Create _transformation_decisions if it doesn't already exist, and grant
    SELECT to app_reader.

    One row per (table_name, candidate_id) — the durable decision cache behind
    Spec 1, Part 1: once a human answers present_transformation_options for a
    given candidate, the answer is reused silently for every future question
    touching that same candidate, until invalidate_cached_decisions_for_table
    clears it on a genuine reload.
    """
    app_reader = os.environ.get("PG_APP_READER_USER", "app_reader")
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS _transformation_decisions (
                table_name       TEXT NOT NULL,
                candidate_id     TEXT NOT NULL,
                chosen_option_id TEXT NOT NULL,
                reasoning_shown  JSONB NOT NULL,
                decided_at       TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (table_name, candidate_id)
            );
            """
        )
        cur.execute(f'GRANT SELECT ON _transformation_decisions TO "{app_reader}";')
    conn.commit()


def write_transformation_decision(conn, table_name: str, candidate_id: str, decision: dict) -> None:
    """Persist one present_transformation_options() answer. `decision` is
    {"chosen_option_id": str, "reasoning_shown": dict} — the full context and
    options shown are preserved (reasoning_shown) so the decision is fully
    reconstructable later, not just "user said B" (Spec 1, Part 1)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO _transformation_decisions
                (table_name, candidate_id, chosen_option_id, reasoning_shown, decided_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (table_name, candidate_id) DO UPDATE SET
                chosen_option_id = EXCLUDED.chosen_option_id,
                reasoning_shown = EXCLUDED.reasoning_shown,
                decided_at = EXCLUDED.decided_at
            """,
            (table_name, candidate_id, decision["chosen_option_id"],
             psycopg2.extras.Json(decision["reasoning_shown"])),
        )
    conn.commit()


def read_transformation_decision(conn, table_name: str, candidate_id: str) -> "dict | None":
    """Return the cached decision for (table_name, candidate_id), or None if
    this candidate has never been decided (or was invalidated by a reload).

    Includes the real decided_at timestamp (Spec 2, Part B) — needed to
    narrate a reused decision honestly ("this had already been decided
    earlier") rather than presenting it as freshly asked in this run.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT chosen_option_id, reasoning_shown, decided_at FROM _transformation_decisions
            WHERE table_name = %s AND candidate_id = %s
            """,
            (table_name, candidate_id),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return {
        "chosen_option_id": row[0],
        "reasoning_shown": row[1],
        "decided_at": row[2].isoformat() if row[2] is not None else None,
    }


def invalidate_cached_decisions_for_table(conn, table_name: str) -> None:
    """Clear every cached Transformation Options decision for table_name.
    Called on a genuine reload/re-clean (Spec 1, Part 0): rather than guessing
    whether an old decision still applies to possibly-different underlying
    data, the simplest safe default is to invalidate everything and let
    surface_relevant_transformations ask again next time a question needs it."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM _transformation_decisions WHERE table_name = %s", (table_name,))
    conn.commit()


def ensure_derived_columns_table(conn) -> None:
    """Create _derived_columns if it doesn't already exist, and grant SELECT
    to app_reader.

    Spec 1, Part 7: every column added by a Transformation Options fix
    (feature derivation, range decomposition, label simplification counts as
    a rewrite of an existing column rather than a new one, so it doesn't need
    an entry) is marked here as "derived, not source" — one row per
    (table_name, column_name) — so add_context can annotate it in the schema
    context `generate_sql` sees, and neither `generate_sql` nor any
    disclosure logic ever mistakes a computed value for an observed one.
    """
    app_reader = os.environ.get("PG_APP_READER_USER", "app_reader")
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS _derived_columns (
                table_name  TEXT NOT NULL,
                column_name TEXT NOT NULL,
                kind        TEXT NOT NULL,
                derived_at  TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (table_name, column_name)
            );
            """
        )
        cur.execute(f'GRANT SELECT ON _derived_columns TO "{app_reader}";')
    conn.commit()


def mark_derived_columns(conn, table_name: str, column_names: list, kind: str) -> None:
    """Record that `column_names` in `table_name` were added by a
    Transformation Options fix of the given `kind` (e.g.
    "range_decomposition", "company_age") — never an observed source value.
    Upserts one row per column; safe to call again after a reload (a fresh
    detection+apply cycle just re-marks the same columns)."""
    with conn.cursor() as cur:
        for column_name in column_names:
            cur.execute(
                """
                INSERT INTO _derived_columns (table_name, column_name, kind, derived_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (table_name, column_name) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    derived_at = EXCLUDED.derived_at
                """,
                (table_name, column_name, kind),
            )
    conn.commit()


def get_derived_columns(conn, table_name: str) -> dict:
    """Returns {column_name: kind} for every column of `table_name` marked
    derived — empty dict if none. Used by add_context to annotate the schema
    context it builds for generate_sql."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, kind FROM _derived_columns WHERE table_name = %s",
            (table_name,),
        )
        rows = cur.fetchall()
    conn.commit()
    return dict(rows)


def compute_file_checksum(path) -> str:
    """Real SHA-256 hex digest of path's actual bytes, streamed in chunks so this
    works for arbitrarily large files without loading them fully into memory."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_stored_checksum(conn, table_name: str) -> str | None:
    """Return the source_checksum last recorded for table_name in _data_quality_status,
    or None if there's no row yet for this table, or the column is NULL (a row written
    before checksum tracking existed) — both cases mean "no baseline to compare"."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_checksum FROM _data_quality_status WHERE table_name = %s",
            (table_name,),
        )
        row = cur.fetchone()
    # Commit immediately so this read-only SELECT doesn't hold a lock across whatever
    # the caller does next (e.g. an ALTER TABLE during the same admin session) — same
    # pattern used elsewhere in this codebase for exactly this reason.
    conn.commit()
    return row[0] if row else None


def check_source_freshness(conn, table_name: str, csv_path) -> tuple:
    """Compare csv_path's CURRENT sha256 against the checksum stored for table_name.

    Returns (source_changed, current_checksum). source_changed is True ONLY when a
    prior checksum already exists for table_name AND it differs from the file's
    current bytes — i.e. the raw source file has genuinely been modified on disk
    since table_name was last processed. A table with no stored checksum yet (first
    time it's ever been processed) is NOT reported as "changed" — there's nothing to
    compare it against.

    This is a detection/audit signal, not a gate: whatever calls clean_dataset()
    afterward always re-examines the file's real, current content regardless of this
    result — see the callers in clean_and_reload and load_data.py's main() for why
    that distinction matters (avoiding a silent, stale-looking reuse of an old result).
    """
    current_checksum = compute_file_checksum(csv_path)
    stored_checksum = get_stored_checksum(conn, table_name)
    source_changed = stored_checksum is not None and stored_checksum != current_checksum
    return source_changed, current_checksum


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


def _create_and_populate_table(conn, table_name: str, csv_path: Path, sample_rows_for_typing: int = 500) -> int:
    """Create table_name fresh and load every row of csv_path into it. Returns the
    row count loaded. Raises on any failure (malformed row, type mismatch, etc.) —
    callers are responsible for conn.rollback() on exception; this function never
    commits, so an aborted call leaves nothing durable behind.

    This is the low-level create+populate primitive shared by load_csv_to_table's
    staging-table step (see below) and anything else that wants a plain table build
    without the atomic-swap machinery.
    """
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
        # DROP-then-CREATE here is only a self-heal for a leftover staging table from
        # a prior crashed load (same deterministic staging name reused) — it never
        # touches the real, live table, which always keeps its own separate name until
        # the atomic swap in load_csv_to_table renames this one into place.
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

    return row_count


def _swap_table_atomically(conn, staging_name: str, table_name: str) -> None:
    """Atomically replace table_name's content with staging_name's, entirely within
    the caller's still-open transaction (no commit happens here — load_csv_to_table
    commits once, after this returns, so table population and the swap live in one
    all-or-nothing transaction).

    Any existing table_name is renamed to table_name_previous — kept as one
    generation of rollback (see rollback_table) rather than dropped — before
    staging_name is renamed into table_name's place. ALTER TABLE ... RENAME is atomic
    DDL in Postgres, so a concurrent reader querying table_name never observes it
    missing or half-populated: it sees either the complete old table (pre-commit) or
    the complete new one (post-commit), never a gap.
    """
    previous_name = f"{table_name}_previous"
    with conn.cursor() as cur:
        # Table/staging names here are derived from sanitize_identifier() output plus
        # fixed literal suffixes — not user/network input — same rationale as the
        # identifier-building elsewhere in this module.
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table_name,))
        table_exists = cur.fetchone()[0]

        cur.execute(f'DROP TABLE IF EXISTS "{previous_name}" CASCADE;')
        if table_exists:
            cur.execute(f'ALTER TABLE "{table_name}" RENAME TO "{previous_name}";')
        cur.execute(f'ALTER TABLE "{staging_name}" RENAME TO "{table_name}";')


def load_csv_to_table(conn, csv_path: Path, sample_rows_for_typing: int = 500):
    """Load csv_path into Postgres as table sanitize_identifier(csv_path.stem),
    replacing any existing table of that name ATOMICALLY (architecture review point
    #20 — Tier 4 hardening of the one workflow in this project that mutates real, live
    data).

    Two-phase, crash-safe design, all inside ONE transaction (single conn.commit() at
    the very end):
      1. Build and fully populate a distinctly-named STAGING table
         (_create_and_populate_table). If anything goes wrong here — a malformed row,
         a type mismatch, any exception — the whole transaction is rolled back below:
         no staging table is left behind, and the real target table (if it already
         exists) was never touched at all, so it stays completely intact and
         queryable throughout.
      2. Only once phase 1 fully succeeds, atomically swap the staging table into
         place (_swap_table_atomically): the existing table_name (if any) becomes
         table_name_previous — one generation of rollback, see rollback_table() —
         and the staging table becomes table_name.

    Returns (table_name, row_count) — same public contract as before this change.
    """
    table_name = sanitize_identifier(csv_path.stem)
    # Deterministic (not random) staging name: a distinctive, unlikely-to-collide
    # prefix rather than a uuid, so a staging table orphaned by a crash mid-load is
    # automatically cleaned up by the next load's own DROP-then-CREATE (see
    # _create_and_populate_table) instead of accumulating garbage tables forever.
    # This project only ever runs one load of a given table at a time (a manual
    # script run, or one clean_and_reload node execution) — concurrent loads of the
    # SAME table were never a supported scenario this needs to guard against.
    staging_name = f"__reload_staging__{table_name}"

    try:
        row_count = _create_and_populate_table(conn, staging_name, csv_path, sample_rows_for_typing)
        _swap_table_atomically(conn, staging_name, table_name)
    except Exception:
        conn.rollback()
        raise

    conn.commit()
    return table_name, row_count


def rollback_table(conn, table_name: str) -> bool:
    """Restore table_name from table_name_previous — the one generation of rollback
    kept by load_csv_to_table's atomic swap (_swap_table_atomically).

    This is a MANUAL, deliberate recovery action for use after a problem is
    discovered in a freshly-reloaded table. Nothing in this codebase calls it
    automatically — it exists to be run by hand (e.g. from a REPL or a one-off
    script) once someone has decided the current table is wrong and the prior
    version should come back.

    Performs a genuine SWAP (not a destructive overwrite): table_name and
    table_name_previous trade places using a temporary holding name, all inside one
    transaction. This means the just-rolled-back-from table isn't lost either — it
    becomes the new table_name_previous, so a second rollback_table() call would
    swap back again.

    Returns True if a rollback was performed, False if there was no table_name_previous
    to roll back to (nothing changes in that case).
    """
    previous_name = f"{table_name}_previous"
    holding_name = f"{table_name}_rollback_tmp"
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (previous_name,))
        previous_exists = cur.fetchone()[0]
        if not previous_exists:
            conn.rollback()
            return False

        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (table_name,))
        current_exists = cur.fetchone()[0]

        cur.execute(f'DROP TABLE IF EXISTS "{holding_name}" CASCADE;')
        if current_exists:
            cur.execute(f'ALTER TABLE "{table_name}" RENAME TO "{holding_name}";')
        cur.execute(f'ALTER TABLE "{previous_name}" RENAME TO "{table_name}";')
        if current_exists:
            cur.execute(f'ALTER TABLE "{holding_name}" RENAME TO "{previous_name}";')
    conn.commit()
    return True


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

    conn = get_admin_connection()
    try:
        ensure_data_quality_status_table(conn)
        ensure_fanout_status_table(conn)
        ensure_transformation_candidates_table(conn)
        ensure_transformation_decisions_table(conn)
        ensure_derived_columns_table(conn)

        # Source-checksum freshness check (architecture review point #20): before
        # this manual re-clean runs, compare each file's CURRENT raw bytes against the
        # checksum recorded the last time its table was processed. clean_dataset()
        # below always re-examines the file's real, current content regardless of
        # this result — this is a deliberate detection/audit step, so a maintainer
        # re-running this script never mistakes what's about to happen for a stale,
        # reused result when the source file has actually changed since it was last
        # loaded.
        for csv_path in csv_files:
            table_name = sanitize_identifier(csv_path.stem)
            changed, _current_checksum = check_source_freshness(conn, table_name, csv_path)
            if changed:
                print(
                    f"[checksum] source changed, forcing fresh clean for "
                    f"'{table_name}' ({csv_path}).",
                    file=sys.stderr,
                )

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

        load_plan = []  # list[(csv_path_to_actually_load, original_name, raw_csv_path)]
        for csv_path in csv_files:
            if csv_path.name in attempted_names:
                load_plan.append((Path(cleaning_result.cleaned_dir) / csv_path.name, csv_path.name, csv_path))
            else:
                load_plan.append((csv_path, csv_path.name, csv_path))

        print(f"\nLoading {len(load_plan)} file(s) into Postgres...")
        for load_path, original_name, raw_csv_path in load_plan:
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
            # Checksum is always computed from the RAW source file (never the
            # cleaned/ clone) — it tracks whether the source has changed, not
            # whether cleaning changed its output.
            write_data_quality_status(
                conn, table_name, status, issues_found, was_cleaned,
                source_folder=str(folder),
                source_checksum=compute_file_checksum(raw_csv_path),
            )
            print(f"  -> data quality status: {status} ({len(issues_found)} unresolved issue(s))")
            compute_and_write_fanout_status(conn, table_name)
            print(f"  -> fan-out status computed for '{table_name}'")

            # Spec 1, Part 0: (re-)detect Transformation Options candidates on this
            # fresh load and invalidate any previously-cached decisions for this
            # table — a genuine reload never guesses whether an old decision still
            # applies to possibly-different underlying data (Part 0's reload-
            # invalidation rule).
            from utils.transformation_options import detect_transformation_candidates

            loaded_df = _read_csv_robust(load_path)
            candidates = detect_transformation_candidates(loaded_df, table_name)
            write_transformation_candidates(conn, table_name, candidates)
            invalidate_cached_decisions_for_table(conn, table_name)
            print(f"  -> {len(candidates)} transformation candidate(s) detected for '{table_name}'")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
