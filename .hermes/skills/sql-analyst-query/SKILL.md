---
name: sql-analyst-query
description: "Answer data questions on the loaded dataset via main.py."
version: 0.1.0
author: Dylan Gagliordi, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [sql, data-agent, langgraph, project-local]
    related_skills: []
---

# SQL Analyst Query Skill

Answers business/data questions about whatever dataset is currently loaded into
this project's Postgres database, by invoking the project's own SQL analyst
LangGraph sub-agent — never by answering from the model's own knowledge or by
guessing at what the data contains.

**TEMPORARY MEASURE:** this skill currently calls the SQL analyst sub-agent
(`main.py`) directly. Once a router sub-agent exists in this project to
classify incoming questions (SQL analyst vs ETL analyst vs anything else),
this skill must be updated to call the router instead, so data questions and
ETL requests get dispatched correctly rather than everything being funneled
through the SQL analyst unconditionally.

## When to Use

- **Deterministic trigger — always applies, no judgment call:** the user's
  message starts with `ask:` (case-insensitive, optional leading/trailing
  whitespace around the colon). Treat everything after the prefix as the
  question, verbatim, and run it through `main.py`. Do not evaluate whether it
  "looks like" a data question first — the prefix alone is sufficient and
  final.
- **Fallback (looser) trigger:** with no `ask:` prefix, use this skill when the
  message is still obviously a business or data question about the dataset
  currently loaded in `data_agent_db` (counts, aggregates, joins, breakdowns,
  "how many X", "what's the average Y", "top N by Z", etc.).
- Don't use for: questions about the project's code, schema design discussions,
  or requests to load/change the dataset itself (that's `utils/load_data.py`,
  run directly, not through this skill).

## Prerequisites

- Run from the project root (`data-agent/`) — the CLI script resolves table
  context, models, and DB credentials relative to that directory.
- Postgres must be running with `data_agent_db` populated (see
  `utils/load_data.py`) and the `app_reader` role configured.
- `ANTHROPIC_API_KEY` and the `PG_*` connection vars must already be present in
  the profile's `.env` — this skill does not set them up.

## How to Run

Invoke the question through the `terminal` tool, from the project root:

```
terminal(command='python main.py "<the user's question>"', workdir="<project root>")
```

`main.py` prints exactly one thing on success: the plain-English final answer.
Nothing else (no SQL, no raw state) is on stdout.

## Procedure

1. If the message starts with `ask:`, strip that prefix (and surrounding
   whitespace) and use the remainder as the question verbatim — no further
   interpretation needed. Otherwise, take the user's question as literally as
   possible — don't pre-interpret or answer it yourself first.
2. Run `python main.py "<question>"` via the `terminal` tool from the project
   root. Quote the question so shell word-splitting doesn't mangle it.
3. Read the real stdout the command returned.
4. Relay that exact answer back to the user as your response (you may lightly
   format it, but do not add facts, numbers, or caveats that didn't come from
   the tool's own output).
5. If the command errors or produces no output, tell the user the query
   failed and show the real error — do not fall back to answering from your
   own knowledge of the dataset.

Completion criterion: your response to the user's data question is derived
entirely from the actual stdout of a real `python main.py "..."` invocation
you just ran in this session, not from prior knowledge of the dataset.

## Pitfalls

- Never answer a data question about the loaded dataset from memory or by
  guessing plausible-looking numbers — always actually invoke the script.
- If the question is ambiguous about which dataset/table it means, still run
  it through `main.py` — the SQL analyst has live schema context and will
  either answer correctly or its own safety/error handling will report the
  real problem.
- `main.py` can take a while (multiple LLM calls: curate, generate, judge,
  execute, possibly retries, then summarize) — don't assume a delay means
  failure.

## Verification

- The terminal output actually shows a printed answer (not a traceback, not
  the usage message).
- The number/fact you relay to the user matches the tool's stdout verbatim in
  substance — you did not add or change any figure.
