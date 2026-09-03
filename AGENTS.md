# Project Standing Rules

These apply to every task in this project, not just the current one.

## Environment
- Use `uv` for all Python package management — never `pip` directly.
- Postgres has two separate roles. `app_reader` is read-only and is what the running agent
  uses. The admin/superuser connection is only ever used by `utils/load_data.py`. Never widen
  `app_reader`'s permissions to make something work — that defeats its purpose.

## Secrets
- All credentials live in `.env`, never hardcoded in source files.
- Never print, log, or include a secret in test output — including database passwords, not
  just API keys.

## SQL
- Always use parameterized queries (placeholders, never string-built SQL) — this applies to
  every query in the codebase, including internal schema-introspection queries, not just
  user-facing ones.

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
