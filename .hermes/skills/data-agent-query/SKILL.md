---
name: data-agent-query
description: "Router-dispatched data/ETL requests via main.py."
version: 0.2.0
author: Dylan Gagliordi, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [sql, etl, router, data-agent, langgraph, project-local]
    related_skills: []
---

# Data Agent Query Skill

Answers business/data questions about whatever dataset is currently loaded into
this project's Postgres database, AND handles ETL-style requests (downloading
data from a URL, cleaning/transforming a folder of raw data files) — both by
invoking the project's own router-orchestrated `main.py`, never by answering
from the model's own knowledge, guessing at what the data contains, or
manually deciding which sub-agent should handle the request.

As of the router (Chapter 18), `main.py` no longer calls the SQL analyst
directly. It invokes the top-level `data_agent` graph, whose `router_node`
classifies every incoming message as `sql_analyst` or `etl_analyst` and
dispatches to the right sub-agent automatically. This skill's job is simply to
get the user's raw text into `main.py` correctly and relay back whatever it
prints — the classification and dispatch decision belongs entirely to the
router, not to this skill or to you. (This skill used to be named
`sql-analyst-query` and called the SQL analyst directly, back before the
router existed — see its earlier revision if you need that history.)

## When to Use

- **Deterministic trigger — always applies, no judgment call:** the user's
  message starts with `ask:` (case-insensitive, optional leading/trailing
  whitespace around the colon). Take everything after the prefix — the raw
  text, character for character — and pass it to `main.py` exactly as
  written. Do not evaluate whether it "looks like" a data question or an ETL
  request first — the prefix alone is sufficient and final; the router itself
  decides which sub-agent handles it.
- **Fallback (looser) trigger:** with no `ask:` prefix, use this skill when the
  message is still obviously either:
  - a business or data question about the dataset currently loaded in
    `data_agent_db` (counts, aggregates, joins, breakdowns, "how many X",
    "what's the average Y", "top N by Z", etc.), OR
  - an ETL-style request: downloading/fetching data from a URL into a local
    folder, or cleaning/transforming/preparing a folder of raw data files.
- Don't use for: questions about the project's code, schema design discussions,
  or requests to load a dataset into the database for the first time (that's
  `utils/load_data.py`, run directly, not through this skill or `main.py`).

## CRITICAL: never clean up, rephrase, or interpret the message at this layer

Whichever sub-agent ends up handling a message, the message itself must reach
`main.py` completely unmodified (aside from the exceptions below). For a
SQL-routed message, the SQL analyst's own `curate_question` node is the one
and only place wording cleanup is allowed to happen — and every cleanup it
makes is captured in `logs/query_log.jsonl` as `curated_question`, next to the
original `user_question`, so it's visible and auditable. For an ETL-routed
message, there is no equivalent cleanup node at all — the raw text goes
straight to the ETL analyst's ReAct loop.

This means: whatever the user typed after `ask:` — however messy, rambling,
typo-filled, uncertain, or grammatically broken — goes into the `main.py`
command AS-IS. Do not:
- fix spelling, grammar, or punctuation
- rewrite it into a "cleaner" or more formal request
- resolve the user's own uncertainty for them (e.g. "or the state idk" stays
  in, don't silently pick one interpretation and drop the hedge)
- summarize, shorten, or restate what you think they meant
- add clarifying words they didn't type
- decide for yourself whether it's a SQL question or an ETL request — that is
  the router's job, not yours; pass it through and let `route_response` in
  the log tell you which way it went

If you do any of this, the cleanup/routing layer that's supposed to happen
inside the graph is silently bypassed, the log no longer reflects what the
user actually asked, and the user loses the ability to see (and correct) how
their request was interpreted or routed.

There is exactly ONE narrow exception to this — see the next section. It does
not weaken anything above: a message that is a complete, standalone request on
its own (however messy or informal) is ALWAYS passed through untouched, no
exceptions, full stop.

## The one exception: context-dependent follow-ups

`main.py` invokes a fresh graph with no memory of anything said earlier in
this conversation. A message like "what about for RJ instead" or "same thing
but for last year" is not messy or informal — it is genuinely incomplete on
its own, and passing it verbatim would send `main.py` a fragment it cannot
possibly answer or act on (it has no "it" or "that" to refer back to). This is
a real, different problem from the wording-cleanup case above, and it is the
only case where combining text from an earlier message is legitimate.

**Case 1 — standalone request (the default, the common case):** the message
makes complete sense on its own, even if messy, vague, rambling, or missing
minor details. Pass it to `main.py` exactly as written. This is the existing
verbatim rule — it is not weakened or reopened for judgment calls.

**Case 2 — context-dependent follow-up:** the message is only interpretable
using an earlier message in this same conversation. Concrete signals:
- it uses a pronoun/reference with no antecedent of its own ("it", "that",
  "those", "this one")
- it's explicitly a variation of a prior ask ("instead", "what about",
  "same thing but", "and for X too")
- it omits a subject entirely and only makes sense as a delta on the last
  message (e.g. just "for RJ?" right after a question about SP)

When (and only when) case 2 clearly applies: combine the necessary context
from the earlier message with the new one into one complete, self-contained
message. Do not invent details neither message contained — pull only what's
needed to make the fragment stand on its own.

**Default rule when unsure:** if it is not clearly and unambiguously case 2,
treat it as case 1. Never guess that context-combination is needed — that
guess is exactly the kind of silent reinterpretation this skill exists to
prevent. When in doubt, pass it through as-is.

**Mandatory transparency (no exceptions) whenever case 2 applies:** before
running `main.py`, show the user the reconstructed message as its own
explicit line, in this exact form:

```
Reading this as: <the reconstructed, self-contained message>
```

Then run `main.py` with that reconstructed text. This line must appear every
single time reconstruction happens — it is the safeguard that makes
reconstruction visible instead of silent, which is the entire reason this
exception is allowed to exist at all. Case 1 never gets this line — only
case 2 does, because only case 2 involved changing what gets sent.

## Prerequisites

- Run from the project root (`data-agent/`) — `main.py` resolves the router
  graph, both sub-agent graphs, models, and DB credentials relative to that
  directory.
- Postgres must be running with `data_agent_db` populated (see
  `utils/load_data.py`) and the `app_reader` role configured — needed for any
  message that routes to the SQL analyst.
- `ANTHROPIC_API_KEY` and the `PG_*` connection vars must already be present in
  the profile's `.env` — this skill does not set them up.
- For an ETL-routed request that triggers a cleaning step, `main.py` will
  print the generated cleaning code and block on a real terminal prompt for
  approval — this only happens when running `main.py` directly in an
  interactive terminal, not via this skill's `terminal` tool call (which
  cannot answer an interactive prompt); a cleaning approval mid-run will hang
  the command. If a request is expected to trigger cleaning, warn the user
  this needs to be run interactively rather than through this skill.

## How to Run

Invoke the message through the `terminal` tool, from the project root, with
the user's raw text substituted in unmodified:

```
terminal(command='uv run python main.py "<RAW user text, unmodified, after stripping only the ask: prefix if present>"', workdir="<project root>")
```

`main.py` prints exactly one thing on success: the plain-English final answer
from whichever sub-agent the router dispatched to. Nothing else (no SQL, no
raw state, no mention of which sub-agent ran) is on stdout — check
`logs/query_log.jsonl`'s `route_response` field if you need to know which way
a request was routed.

## Procedure

1. If the message starts with `ask:`, strip ONLY that literal prefix (and any
   whitespace immediately touching the colon) — nothing else about the text
   changes yet.
2. Decide case 1 vs case 2 (see "The one exception" above). Default to case 1
   when unsure.
   - Case 1 (standalone): the text from step 1, untouched, is the message.
   - Case 2 (context-dependent follow-up): combine it with the necessary
     context from the earlier message into one self-contained message, and
     output the line `Reading this as: <reconstructed message>` before
     proceeding — this is not optional and applies every time case 2 fires.
3. Run `uv run python main.py "<message from step 2>"` via the `terminal` tool
   from the project root. Quote it so shell word-splitting doesn't mangle it,
   but do not otherwise alter it.
4. Read the real stdout the command returned.
5. Relay that exact answer back to the user as your response (you may lightly
   format it, but do not add facts, numbers, or caveats that didn't come from
   the tool's own output).
6. If the command errors, produces no output, or hangs on an interactive
   approval prompt (see Prerequisites), tell the user the run failed/needs an
   interactive terminal and show the real error — do not fall back to
   answering from your own knowledge or guessing what would have happened.

Completion criterion: your response to the user is derived entirely from the
actual stdout of a real `python main.py "..."` invocation you just ran in this
session, with either (a) the user's raw, unmodified text as the argument
(case 1), or (b) a reconstructed message that was shown to the user via the
`Reading this as:` line before running (case 2) — never a silently rewritten
version of what you think they meant, and never a routing decision you made
yourself instead of the router.

## Pitfalls

- Never answer a data question about the loaded dataset from memory or by
  guessing plausible-looking numbers — always actually invoke `main.py`.
- Never decide yourself whether a message is SQL-shaped or ETL-shaped and
  route it differently based on that guess — that decision belongs entirely
  to `router_node` inside the graph, every time, with no exceptions. This
  skill's only job is getting the raw text to `main.py` correctly.
- Never "clean up" or paraphrase a standalone (case 1) message before it
  reaches `main.py`, even when it's genuinely messy, uncertain, or informally
  worded — see the CRITICAL section above.
- Don't confuse "messy/informal" with "context-dependent" — a rambling but
  self-contained message is still case 1 and still goes through untouched.
  Case 2 is narrowly about fragments that are genuinely unanswerable without
  an earlier message (pronouns with no antecedent, explicit deltas like
  "instead" or "what about").
- Never skip the `Reading this as:` line when case 2 applies, and never show
  it when case 1 applies (it would be noise / imply a change that didn't
  happen).
- If the message is ambiguous about which dataset/table/URL it means, still
  run it through `main.py` verbatim — the router and both sub-agents have
  their own real reasoning and error handling and will either succeed or
  report the real problem.
- `main.py` can take a while (multiple LLM calls across the router and
  whichever sub-agent it dispatches to, possibly retries) — don't assume a
  delay means failure.
- An ETL-routed request that needs a cleaning approval will block on a real
  terminal prompt this skill's `terminal` tool call cannot answer — see
  Prerequisites.

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
- `logs/query_log.jsonl`'s `route_response` field for that run tells you which
  sub-agent actually handled the request (`sql_analyst` or `etl_analyst`) —
  use this to confirm the router's decision if the user asks, rather than
  guessing which sub-agent ran from the answer text alone.
