---
name: data-agent-artifact-standard
description: The fixed content and process standard for rebuilding the published "Data Agent Blueprint" architecture artifact — read this before every rebuild, not SKILL.md (that one is for me, mid-task; this one is for the artifact's actual reader).
metadata:
  type: reference
---

# Artifact Standard — Data Agent Blueprint

This is the companion to `SKILL.md` for one specific document: the published
architecture artifact ("Data Agent Blueprint"). `SKILL.md` is written for me,
mid-task, and assumes I already know this codebase. This standard is written
for whoever rebuilds the artifact, and governs a document whose reader is the
opposite of me: **someone with zero technical background**, reading this to
understand what the project actually does and how, from nothing.

Follow this exactly on every rebuild — the goal is a rebuild in six months, by
a different session with no memory of this one, producing a structurally
consistent result because it's following a written standard, not because it
remembers a conversation.

## 1. Audience and bar (Spec 13, non-negotiable)

Write for a non-technical reader from the ground up. Concretely:

- **Never assume a term.** "LLM," "node," "graph," "foreign key," "AST,"
  "sandboxing," "Postgres role," "approval gate" — every one of these must be
  taught, briefly, in plain language, the first time it's used. Not a
  footnote, not a glossary entry the reader has to go find — a sentence or
  two right where the term first appears, then use it freely after that.
- **Explain the *why*, not just the *what*.** "The database has two roles"
  says nothing to a non-technical reader. "This project uses two separate
  database logins — one that can only look at data, and one that can change
  it — so that even if the AI is tricked into writing a harmful command, the
  database itself physically refuses to run it" says something.
- **Concrete beats abstract.** Prefer a real example (an actual question,
  an actual generated SQL query, an actual chart) over a description of the
  general case. Every sheet in the current artifact already does this well
  in places (e.g. the decline-sentinel table) — extend that instinct
  everywhere, not just where it already happened to land.

## 2. Structure: a Foundations sheet, then the rest

Add a new **Sheet 00 — Foundations**, positioned before the current Sheet 00
(System overview), teaching the handful of concepts nearly every later sheet
depends on, once, so later sheets can use them freely instead of re-explaining
each time:

- What an LLM call actually is (a request with text in, text out — not magic,
  not a lookup, a model guessing the most plausible continuation).
- What a database and a table are, in plain terms.
- What "a graph of nodes" means here — a flowchart the code actually follows,
  not a chart/graph in the data-visualization sense (this exact ambiguity has
  already come up and needed explaining once in conversation — don't make a
  reader hit the same confusion unaided).
- What "human-in-the-loop" / an approval gate means, and why one exists at
  all (so a reader isn't surprised later when a sheet says "the system pauses
  and asks a human").

Every later sheet may then use these terms freely, with at most a one-clause
reminder ("an LLM call — see Foundations") rather than a full re-explanation.

**Required sheet list** (cross-check against `python main.py "inventory"`
before publishing — a sheet must exist for everything real currently in the
codebase; nothing gets silently dropped):

0. Foundations (new)
1. System overview
2. SQL Analyst graph
3. ETL Analyst
4. Auto-cleaning pipeline
5. Transformation Options & Manual Mode
6. Reports, slideshows & notebook export
7. Governance & Reporting (Spec 8: Semantic Layer, Taxonomy Governance,
   Rubric Dashboard, Lineage/Explain, Audit Export)
8. Analysis Expansion (Spec 9: Auto-EDA, Join Advisory, Significance Testing)
9. Ingestion Expansion (Spec 10: Format Normalization, Scraping, Ingestion
   Source Registry)
10. Scratch Mode (Spec 12)
11. Code map
12. DB & security boundaries

(Numbering is illustrative, not sacred — what's non-negotiable is that every
real capability in the inventory has a home somewhere in the sheet list, not
the exact numbers.)

## 3. Diagrams: one per subsection, not one per sheet

The current artifact draws one Mermaid diagram per whole sheet (e.g. one
diagram for the entire SQL analyst graph, 15+ nodes at once). That asks a
first-time reader to hold the whole system in their head before any single
piece makes sense.

Going forward: **every meaningfully distinct sub-concept within a sheet gets
its own small diagram**, sized to that one idea. Concretely, a sheet like
"SQL Analyst graph" should draw separately:
- the plain-question path (curate → context → generate → safety → execute →
  answer) on its own, small and simple;
- the auto-clean redirect loop, on its own;
- the visualization finishing path (resolve columns → validate shape → decide
  scratch-vs-fixed → render), on its own;
- the Scratch Mode safety-then-approval sequence, on its own.

A reader should be able to understand each small diagram completely before
moving to the next, and only see the full combined picture (if at all) after
the pieces already make sense individually.

## 4. Visual design system — preserve it, don't reinvent it

The existing artifact's visual identity is good and deliberate — a drafting
sheet/blueprint motif (`--paper`, `--ink`, `--line-strong` tokens; the
`.sheet[data-tag]` numbered-sheet convention; the `tag.sql`/`tag.etl`/
`tag.viz` lane-coloring system; `IBM Plex Sans` / `Big Shoulders Display` /
`IBM Plex Mono`). Keep it. A rebuild's job is deeper, more accessible
*content* inside this system, not a new visual identity — reusing it is also
literally how "created the same way every time" is enforced for anything a
written checklist can't fully pin down (tone, spacing judgment calls, etc.).

## 5. Pre-publish check

Before publishing any rebuild, fetch `python main.py "inventory"`'s real
output and confirm: every graph node it lists appears somewhere in the
relevant sheet; every `utils/` module it lists is mentioned in the Code Map
sheet; every CLI command it lists appears in the System Overview sheet's
command table. A rebuild that fails this check is not done, regardless of how
much content it added.
