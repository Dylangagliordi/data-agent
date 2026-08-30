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
  whitespace around the colon). Take everything after the prefix — the raw
  text, character for character — and pass it to `main.py` exactly as
  written. Do not evaluate whether it "looks like" a data question first —
  the prefix alone is sufficient and final.
- **Fallback (looser) trigger:** with no `ask:` prefix, use this skill when the
  message is still obviously a business or data question about the dataset
  currently loaded in `data_agent_db` (counts, aggregates, joins, breakdowns,
  "how many X", "what's the average Y", "top N by Z", etc.).
- Don't use for: questions about the project's code, schema design discussions,
  or requests to load/change the dataset itself (that's `utils/load_data.py`,
  run directly, not through this skill).

## CRITICAL: never clean up, rephrase, or interpret the question at this layer

`main.py`'s own graph has a dedicated node, `curate_question`, whose entire job
is cleaning up wording — and every cleanup it makes is captured in
`logs/query_log.jsonl` as `curated_question`, next to the original
`user_question`, so the cleanup is visible and auditable. That is the ONLY
place cleanup is allowed to happen.

This means: whatever the user typed after `ask:` — however messy, rambling,
typo-filled, uncertain, or grammatically broken — goes into the `main.py`
command AS-IS. Do not:
- fix spelling, grammar, or punctuation
- rewrite it into a "cleaner" or more formal question
- resolve the user's own uncertainty for them (e.g. "or the state idk" stays
  in, don't silently pick one interpretation and drop the hedge)
- summarize, shorten, or restate what you think they meant
- add clarifying words they didn't type

If you do any of this, `curate_question`'s cleanup step is silently bypassed,
its log entry no longer reflects what the user actually asked, and the user
loses the ability to see (and correct) how their question was interpreted.
Pass the raw text through untouched — that is the whole point of this skill
existing as a thin pass-through rather than a second interpretation layer.

There is exactly ONE narrow exception to this — see the next section. It does
not weaken anything above: a message that is a complete, standalone question
on its own (however messy or informal) is ALWAYS passed through untouched, no
exceptions, full stop.

## The one exception: context-dependent follow-ups

`main.py` invokes a fresh graph with no memory of anything said earlier in
this conversation. A message like "what about for RJ instead" or "same thing
but for last year" is not messy or informal — it is genuinely incomplete on
its own, and passing it verbatim would send `main.py` a fragment it cannot
possibly answer (it has no "it" or "that" to refer back to). This is a real,
different problem from the wording-cleanup case above, and it is the only
case where combining text from an earlier message is legitimate.

**Case 1 — standalone question (the default, the common case):** the message
makes complete sense as its own question, even if messy, vague, rambling, or
missing minor details. Pass it to `main.py` exactly as written. This is the
existing verbatim rule — it is not weakened or reopened for judgment calls.

**Case 2 — context-dependent follow-up:** the message is only interpretable
using an earlier question in this same conversation. Concrete signals:
- it uses a pronoun/reference with no antecedent of its own ("it", "that",
  "those", "this one")
- it's explicitly a variation of a prior ask ("instead", "what about",
  "same thing but", "and for X too")
- it omits a subject entirely and only makes sense as a delta on the last
  question (e.g. just "for RJ?" right after a question about SP)

When (and only when) case 2 clearly applies: combine the necessary context
from the earlier question with the new message into one complete,
self-contained question. Do not invent details neither message contained —
pull only what's needed to make the fragment stand on its own.

**Default rule when unsure:** if it is not clearly and unambiguously case 2,
treat it as case 1. Never guess that context-combination is needed — that
guess is exactly the kind of silent reinterpretation this skill exists to
prevent. When in doubt, pass it through as-is.

**Mandatory transparency (no exceptions) whenever case 2 applies:** before
running `main.py`, show the user the reconstructed question as its own
explicit line, in this exact form:

```
Reading this as: <the reconstructed, self-contained question>
```

Then run `main.py` with that reconstructed text. This line must appear every
single time reconstruction happens — it is the safeguard that makes
reconstruction visible instead of silent, which is the entire reason this
exception is allowed to exist at all. Case 1 never gets this line — only
case 2 does, because only case 2 involved changing what gets sent.

## Prerequisites

- Run from the project root (`data-agent/`) — the CLI script resolves table
  context, models, and DB credentials relative to that directory.
- Postgres must be running with `data_agent_db` populated (see
  `utils/load_data.py`) and the `app_reader` role configured.
- `ANTHROPIC_API_KEY` and the `PG_*` connection vars must already be present in
  the profile's `.env` — this skill does not set them up.

## How to Run

Invoke the question through the `terminal` tool, from the project root, with
the user's raw text substituted in unmodified:

```
terminal(command='python main.py "<RAW user text, unmodified, after stripping only the ask: prefix if present>"', workdir="<project root>")
```

`main.py` prints exactly one thing on success: the plain-English final answer.
Nothing else (no SQL, no raw state) is on stdout.

## Procedure

1. If the message starts with `ask:`, strip ONLY that literal prefix (and any
   whitespace immediately touching the colon) — nothing else about the text
   changes yet.
2. Decide case 1 vs case 2 (see "The one exception" above). Default to case 1
   when unsure.
   - Case 1 (standalone): the text from step 1, untouched, is the question.
   - Case 2 (context-dependent follow-up): combine it with the necessary
     context from the earlier question into one self-contained question, and
     output the line `Reading this as: <reconstructed question>` before
     proceeding — this is not optional and applies every time case 2 fires.
3. Run `python main.py "<question from step 2>"` via the `terminal` tool from
   the project root. Quote it so shell word-splitting doesn't mangle it, but
   do not otherwise alter it.
4. Read the real stdout the command returned.
5. Relay that exact answer back to the user as your response (you may lightly
   format it, but do not add facts, numbers, or caveats that didn't come from
   the tool's own output).
6. If the command errors or produces no output, tell the user the query
   failed and show the real error — do not fall back to answering from your
   own knowledge of the dataset.

Completion criterion: your response to the user's data question is derived
entirely from the actual stdout of a real `python main.py "..."` invocation
you just ran in this session, with either (a) the user's raw, unmodified text
as the argument (case 1), or (b) a reconstructed question that was shown to
the user via the `Reading this as:` line before running (case 2) — never a
silently rewritten version of what you think they meant.

## Pitfalls

- Never answer a data question about the loaded dataset from memory or by
  guessing plausible-looking numbers — always actually invoke the script.
- Never "clean up" or paraphrase a standalone (case 1) question before it
  reaches `main.py`, even when it's genuinely messy, uncertain, or informally
  worded — see the CRITICAL section above. That is `curate_question`'s job,
  inside the graph, where it's logged. Doing it here a second time hides the
  real input from the log and defeats the point of that node.
- Don't confuse "messy/informal" with "context-dependent" — a rambling but
  self-contained question is still case 1 and still goes through untouched.
  Case 2 is narrowly about fragments that are genuinely unanswerable without
  an earlier message (pronouns with no antecedent, explicit deltas like
  "instead" or "what about").
- Never skip the `Reading this as:` line when case 2 applies, and never show
  it when case 1 applies (it would be noise / imply a change that didn't
  happen).
- If the question is ambiguous about which dataset/table it means, still run
  it through `main.py` verbatim — the SQL analyst has live schema context and
  will either answer correctly or its own safety/error handling will report
  the real problem.
- `main.py` can take a while (multiple LLM calls: curate, generate, judge,
  execute, possibly retries, then summarize) — don't assume a delay means
  failure.

## Verification

- The terminal output actually shows a printed answer (not a traceback, not
  the usage message).
- The number/fact you relay to the user matches the tool's stdout verbatim in
  substance — you did not add or change any figure.
- Case 1: the `user_question` argument you actually ran matches what the user
  typed (minus only the `ask:` prefix if present) character for character —
  spot check this against `logs/query_log.jsonl`'s `user_question` field if
  unsure.
- Case 2: the `Reading this as:` line was shown to the user BEFORE `main.py`
  ran, and `logs/query_log.jsonl`'s `user_question` field for that run matches
  the reconstructed text you showed — not the raw fragment, and not something
  different from what you displayed.
