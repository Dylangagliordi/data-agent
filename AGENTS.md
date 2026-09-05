# Project Standing Rules

These apply to every task in this project, not just the current one.

## Environment
- Use `uv` for all Python package management — never `pip` directly.
- Postgres has two separate roles. `app_reader` is read-only and is what the running agent
  uses. The admin/superuser connection is only ever used by `utils/load_data.py`. Never widen
  `app_reader`'s permissions to make something work — that defeats its purpose.

## Database Role Invariant — app_reader is enforced read-only at the DB level

`app_reader` must only hold SELECT on public tables and USAGE on the public schema.
The LLM safety judge (`is_safe`) is a UX/policy layer — the real security boundary is the
database itself. The following grants must never be re-added:

```sql
-- Run once as superuser to establish (or re-establish after any accidental grant):
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON ALL TABLES IN SCHEMA public FROM app_reader;
REVOKE CREATE ON SCHEMA public FROM app_reader;
-- Ensure future tables are also covered:
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON TABLES FROM app_reader;
```

If `app_reader` can INSERT/UPDATE/DELETE/TRUNCATE any table, that is a misconfiguration —
fix it immediately by running the REVOKE commands above as superuser.

## Secrets
- All credentials live in `.env`, never hardcoded in source files.
- Never print, log, or include a secret in test output — including database passwords, not
  just API keys.

## SQL
- Always use parameterized queries (placeholders, never string-built SQL) — this applies to
  every query in the codebase, including internal schema-introspection queries, not just
  user-facing ones.

## Auto-Cleaning/Reload Workflow — the one feature that mutates real, live data

`clean_and_reload` (`agents/sql_analyst.py`) is the only part of this project that can
change what's actually in the database in response to a user's question. It's hardened
as follows — do not weaken any of this without deliberately re-reviewing why it's here:

- **Source checksum tracking.** `_data_quality_status.source_checksum` holds the real
  SHA-256 of a table's raw source file at the time it was last processed
  (`utils/load_data.py:compute_file_checksum` / `check_source_freshness`). Both the
  auto-clean redirect and `load_data.py`'s manual re-clean compare the file's current
  bytes against this before cleaning runs, and log explicitly when they differ — not as
  a gate (cleaning always re-examines the file's real current content regardless), but
  so a changed source is never mistaken for a stale, already-seen result.
- **Versioned cleaned artifacts.** `utils/data_cleaning.py:_clone_file` keeps exactly
  one generation of history: `cleaned/<file>` is the current clone, `cleaned/<file>.previous`
  is the one before it. Bounded, not unlimited.
- **Atomic table replacement.** `utils/load_data.py:load_csv_to_table` never mutates a
  live table in place. It builds a staging table, and only once that fully succeeds
  swaps it into place with `ALTER TABLE ... RENAME` inside one transaction — atomic DDL
  in Postgres, so the real table is never observably missing or half-populated. If the
  staging build fails, the existing table is untouched.
- **Real rollback, one generation.** The swap renames the table being replaced to
  `<table>_previous` instead of dropping it. `utils/load_data.py:rollback_table(conn,
  table_name)` swaps it back. This is a MANUAL, deliberate action — nothing in this
  codebase calls it automatically.
- **Reload coverage semantics (#21):** one `clean_dataset()` call covers every CSV in a
  source folder, but `clean_and_reload` only reloads the tables that were actually in
  `state.tables_to_clean` (the ones with an originally-flagged fail-level status row) —
  not every file that call happened to touch. See the docstring on `clean_and_reload`
  for why: reloading a table that was never flagged as needing it would be redundant
  work, not a correctness fix.

## SSRF Protection — extract_load only ever fetches public network targets

`extract_load` (`agents/etl_analyst.py`) is invoked by an LLM deciding what URL to pass
it, with no allowlist of its own — this is a real, permanent safety boundary, not a
suggestion to be relaxed for convenience:

- Before every fetch, and before following any HTTP redirect, `_validate_fetch_url`
  resolves the target hostname's real IP address(es) and rejects the request if any of
  them fall in a private, loopback, link-local, reserved, multicast, or unspecified
  range (`ipaddress.ip_address(...).is_private` / `.is_loopback` / `.is_link_local` /
  `.is_reserved` / `.is_multicast` / `.is_unspecified`). This is exactly what blocks
  `169.254.169.254` (the AWS/GCP/Azure cloud-metadata endpoint, which serves IAM
  credentials) — it's a link-local address.
- A small set of known metadata hostnames (`metadata.google.internal`, `metadata.goog`)
  are also blocked by name, in case a future metadata service doesn't resolve to a
  link-local IP.
- Only `http`/`https` schemes are allowed.
- Redirects are followed manually (not via `requests`' automatic redirect-following) so
  each redirect target is validated exactly the same way as the original URL — a URL
  that passes validation but redirects to an internal address is still blocked.
- DNS is resolved fresh at request time, not cached or trusted from the URL text alone.

Do not weaken this by adding a bypass flag, widening the allowed IP ranges, or trusting
the URL's hostname without resolving it.

## Workflow
- After building or changing any node or component, write a small test, run it, and show real
  output before moving on to the next piece.
- Commit to git after each verified piece of work, with a message naming what changed.
- If something fails after a few real attempts to fix it, stop and report the issue rather than
  continuing to guess or silently working around it.
- After completing any spec that changes real code structure — new functions, new schema fields,
  new file locations, new established patterns — update
  `.claude/skills/data-agent-architecture/SKILL.md` to reflect the change before considering
  the task finished. This applies every time, automatically, without needing to be asked.
