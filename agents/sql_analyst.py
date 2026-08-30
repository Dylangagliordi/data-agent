"""
SQL analyst sub-agent: LangGraph node definitions.
"""

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, StateGraph

from models.schema import JudgeSchema, SQLAnalystState
from utils.db import get_app_reader_connection
from utils.llm_pick import pick_llm


def _extract_text(content) -> str:
    """Extract plain text from a chat model response's .content.

    Most responses are a plain str, but some providers (confirmed live with
    claude-sonnet-5, which can engage extended thinking even without an
    explicit thinking config) return a list of content blocks instead, e.g.
    [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "..."}].
    Calling .strip() directly on that list crashes with AttributeError. This
    normalizes both shapes down to the actual answer text.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


CURATE_QUESTION_SYSTEM_PROMPT = """You clean up the wording of a raw user question about a \
database. Fix grammar, spelling, and phrasing only. Do NOT change what the question is \
actually asking, do NOT add new constraints, and do NOT answer it. Output only the cleaned-up \
question text, nothing else."""


def curate_question(state: SQLAnalystState) -> dict:
    """Node 1: clean up the raw question's wording only (no LLM reasoning about intent).

    Appends the curated question to messages as a HumanMessage.
    """
    llm = pick_llm("cheap")
    response = llm.invoke(
        [
            ("system", CURATE_QUESTION_SYSTEM_PROMPT),
            ("human", state.user_question),
        ]
    )
    curated = _extract_text(response.content).strip()

    return {
        "curated_question": curated,
        "messages": [HumanMessage(content=curated)],
    }


def add_context(state: SQLAnalystState) -> dict:
    """Node 2 (no LLM): query information_schema live for table/column/type info
    plus 5 sample rows per table currently in the database, and build one context
    string. Table names are never hardcoded — this is fully driven by whatever
    tables actually exist in the public schema at call time.
    """
    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name, column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s
                ORDER BY table_name, ordinal_position
                """,
                ("public",),
            )
            rows = cur.fetchall()

        # Group columns by table, preserving order
        tables: dict[str, list[tuple[str, str]]] = {}
        for table_name, column_name, data_type in rows:
            tables.setdefault(table_name, []).append((column_name, data_type))

        sections = []
        with conn.cursor() as cur:
            for table_name, columns in tables.items():
                col_lines = "\n".join(f"  - {c} ({t})" for c, t in columns)

                # Table identifiers can't be bound as %s placeholders (Postgres only
                # parameterizes values, not identifiers) but table_name here comes
                # straight out of information_schema, not user input, so it's safe
                # to interpolate into the identifier position.
                cur.execute(f'SELECT * FROM "{table_name}" LIMIT %s', (5,))
                sample_rows = cur.fetchall()
                sample_col_names = [desc[0] for desc in cur.description]

                sample_lines = "\n".join(str(dict(zip(sample_col_names, r))) for r in sample_rows)

                sections.append(
                    f"Table: {table_name}\nColumns:\n{col_lines}\nSample rows (up to 5):\n{sample_lines}"
                )

        context = "\n\n".join(sections)
    finally:
        conn.close()

    return {"prompt_query_context": context}


GENERATE_SQL_SYSTEM_PROMPT = """You are a SQL analyst. Given a question and a description of \
the available tables (columns, types, and sample rows), write exactly ONE SQL query that \
answers the question against a PostgreSQL database.

Output ONLY the raw SQL query. No explanation, no commentary, no markdown code fences, no \
backticks — just the SQL statement itself.

Rules:
- If the question asks for a write/action (update, delete, insert, tier assignment, etc.) \
that this read-only query cannot actually perform, do NOT fake it by adding a literal/constant \
column (e.g. `'Gold' AS loyalty_tier`) that pretends the action happened. Write a plain SELECT \
that answers whatever part of the question is genuinely a read (e.g. "which customers spent \
the most"), and leave it at that — do not invent columns representing an action never executed.
- Do not return unbounded full-table results when the question implies a small, specific \
answer (e.g. "the top customer(s)", "which category", "how many") — use LIMIT, aggregation, \
or WHERE clauses so the result set is reasonably sized. Only omit a LIMIT if the question \
genuinely calls for every matching row."""


def _strip_sql_formatting(text: str) -> str:
    """Strip markdown code fences around a SQL query, if the model added them anyway."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        # Drop the opening fence line (``` or ```sql)
        lines = lines[1:]
        # Drop a trailing fence line if present
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


def generate_sql(state: SQLAnalystState) -> dict:
    """Node 3 (high tier): write exactly one SQL query from curated_question + context.

    If this is a retry after a failed execution (sql_query_execution_result holds an
    error), that error is included so the model can try again with real information
    about what went wrong.
    """
    llm = pick_llm("high")

    human_content = (
        f"Question: {state.curated_question}\n\n"
        f"Database context:\n{state.prompt_query_context}"
    )
    if state.sql_query_execution_result:
        human_content += (
            "\n\nA previous attempt at this query failed with this real database error "
            f"— fix the query so it actually works:\n{state.sql_query_execution_result}"
            f"\n\nPrevious (failed) query was:\n{state.generated_sql_query}"
        )

    response = llm.invoke(
        [
            ("system", GENERATE_SQL_SYSTEM_PROMPT),
            ("human", human_content),
        ]
    )
    sql_query = _strip_sql_formatting(_extract_text(response.content))

    return {"generated_sql_query": sql_query}


IS_SAFE_SYSTEM_PROMPT = """You are a security judge reviewing a SQL query. Your ONLY job is \
to decide whether this query is strictly read-only.

Answer "no" if the query contains, anywhere in it, any of: INSERT, UPDATE, DELETE, DROP, \
ALTER, TRUNCATE (in any case, including inside subqueries, CTEs, or comments). Otherwise \
answer "yes".

Do not evaluate correctness, style, or performance — only read-only safety. Give a brief \
reason in comments."""


def is_safe(state: SQLAnalystState) -> dict:
    """Node 4 (medium tier, JudgeSchema via with_structured_output).

    Receives ONLY the generated SQL text — never the original or curated question.
    """
    llm = pick_llm("cheap").with_structured_output(JudgeSchema)
    judgement: JudgeSchema = llm.invoke(
        [
            ("system", IS_SAFE_SYSTEM_PROMPT),
            ("human", state.generated_sql_query),
        ]
    )

    return {"is_safe": judgement.answer, "comments": judgement.comments}


def route_after_safety_check(state: SQLAnalystState) -> str:
    """Conditional edge function for after is_safe.

    Returns a plain string key directly ("execute_sql" or "cancel_sql") based on
    state.is_safe. This return value is passed straight into add_conditional_edges'
    routing mapping — it is NOT written into shared state and read back out, which
    is what would cause a LangGraph concurrent-update error in this graph.
    """
    if state.is_safe == "yes":
        return "execute_sql"
    return "cancel_sql"


MAX_SQL_ATTEMPTS = 5
_SQL_ERROR_PREFIX = "SQL_EXECUTION_ERROR: "
MAX_RESULT_ROWS = 200
_TRUNCATION_MARKER = "[TRUNCATED TO FIRST"


def execute_sql(state: SQLAnalystState) -> dict:
    """Node 6: run generated_sql_query against Postgres using the app_reader role.

    On a real database error (bad column, syntax error, etc.) it does not crash —
    it captures the exact error message (prefixed with a sentinel so the routing
    function can distinguish it deterministically from a real result string) and
    increments the attempt counter so the conditional edge after this node can
    route back to generate_sql with that error, capped at MAX_SQL_ATTEMPTS total
    attempts across the whole cycle.

    Result rows are capped at MAX_RESULT_ROWS: an unbounded query (e.g. one with
    no LIMIT that matches a huge fraction of a table) produced a multi-megabyte
    result string in practice, which the summarizer node could not actually see
    in full and ended up fabricating invented statistics over. Truncation is
    reported explicitly in the result string so downstream nodes never mistake
    a partial result for the complete answer.
    """
    attempts = state.sql_attempts + 1
    conn = get_app_reader_connection()
    try:
        with conn.cursor() as cur:
            # generated_sql_query is model-authored SQL text, not a value to bind —
            # there is no placeholder mechanism for "run this arbitrary statement";
            # safety here is enforced upstream by the is_safe judge gate, not by
            # parameterization (which doesn't apply to whole-statement execution).
            cur.execute(state.generated_sql_query)
            if cur.description is not None:
                col_names = [desc[0] for desc in cur.description]
                rows = cur.fetchmany(MAX_RESULT_ROWS + 1)
                truncated = len(rows) > MAX_RESULT_ROWS
                if truncated:
                    rows = rows[:MAX_RESULT_ROWS]
                result_str = str([dict(zip(col_names, r)) for r in rows])
                if truncated:
                    result_str = (
                        f"{_TRUNCATION_MARKER} {MAX_RESULT_ROWS} ROWS — the query matched more "
                        f"rows than this; the full result set was NOT retrieved, so do not "
                        f"compute counts/averages/min/max over \"all\" rows from this data]\n"
                        f"{result_str}"
                    )
            else:
                result_str = "(query executed, no rows returned)"
        conn.commit()
        return {"sql_query_execution_result": result_str, "sql_attempts": attempts}
    except Exception as e:
        conn.rollback()
        error_str = f"{_SQL_ERROR_PREFIX}{type(e).__name__}: {e}"
        if attempts >= MAX_SQL_ATTEMPTS:
            return {
                "sql_query_execution_result": error_str,
                "sql_attempts": attempts,
                "final_answer": (
                    f"The query could not be completed after {attempts} attempts. "
                    f"The last real database error was: {type(e).__name__}: {e}"
                ),
            }
        return {"sql_query_execution_result": error_str, "sql_attempts": attempts}
    finally:
        conn.close()


def route_after_execute_sql(state: SQLAnalystState) -> str:
    """Conditional edge function for after execute_sql.

    Returns a plain string key: "represent_final_answer" on success or once
    MAX_SQL_ATTEMPTS is reached (final_answer is already set with the error in
    that case by execute_sql itself), otherwise "generate_sql" to retry.
    """
    if state.final_answer:
        # execute_sql already gave up and wrote the final failure answer.
        return "represent_final_answer"
    if state.sql_query_execution_result.startswith(_SQL_ERROR_PREFIX):
        return "generate_sql"
    return "represent_final_answer"


def cancel_sql(state: SQLAnalystState) -> dict:
    """Node 7: write a final_answer explaining the query was blocked, quoting the
    judge's own comments as the reason. Appends this as an AIMessage to messages.
    """
    final_answer = (
        "This query was blocked before execution because it did not pass the "
        f"read-only safety check. Reason given by the safety judge: {state.comments}"
    )
    return {
        "final_answer": final_answer,
        "messages": [AIMessage(content=final_answer)],
    }


REPRESENT_FINAL_ANSWER_SYSTEM_PROMPT = """You take a raw SQL execution result and the \
original question, and write a plain-English answer to what was actually asked.

Rules:
- No SQL, no raw column names dumped as-is — translate into natural language.
- If the execution result is empty, or doesn't clearly answer the question, say so \
plainly instead of forcing an answer.
- Be concise and directly answer the question.
- CRITICAL: only report what the execution result actually shows. The user's original \
question may ask for an action (an update, a deletion, sending something, etc.) that was \
never performed — the SQL that ran may have been read-only (e.g. a SELECT), or blocked \
entirely. Never claim, imply, or hint that any change, update, or write happened unless the \
raw execution result itself explicitly reflects it. If the question asked for an action \
that clearly did not occur, say plainly that only the requested data was retrieved and no \
change was made — do not agree that it happened just because the user asked for it.
- If the execution result starts with "[TRUNCATED TO FIRST", it means the database matched \
more rows than were actually retrieved — you are only seeing a partial slice, not the whole \
result set. In that case, do NOT compute or state any count, average, min, max, or other \
aggregate as if it covers "all" matching rows — that would be fabricated from incomplete data. \
Instead say plainly that the result was too large to fully summarize and describe only the \
partial sample you can actually see (e.g. a few example rows), or suggest the question be \
narrowed (e.g. add a LIMIT or filter) to get a complete answer."""


def represent_final_answer(state: SQLAnalystState) -> dict:
    """Node 8 (low tier): turn the raw execution result into a plain-English answer.

    Appends the final answer as an AIMessage to messages.
    """
    # If execute_sql already gave up after exhausting retries, it already wrote a
    # final_answer explaining the failure — don't overwrite it with an LLM guess.
    if state.final_answer:
        return {
            "final_answer": state.final_answer,
            "messages": [AIMessage(content=state.final_answer)],
        }

    # Deterministic guard, not just a prompt instruction: a small local model was
    # observed (live, reproduced in tests/test_result_truncation.py) to fabricate
    # counts/averages/min/max over a truncated result anyway, despite an explicit
    # system-prompt rule not to. Rather than trust the LLM to comply, skip the LLM
    # summarization entirely when the result is truncated and report the
    # limitation directly — this cannot be talked out of by the model.
    if state.sql_query_execution_result.startswith(_TRUNCATION_MARKER):
        final_answer = (
            "This query matched more rows than could be retrieved in full "
            f"(results are capped at {MAX_RESULT_ROWS} rows), so I can't give you a complete "
            "answer over the full result set — only a partial sample was available. "
            "Try narrowing the question (e.g. ask for a top-N, a specific filter, or an "
            "aggregate computed directly in the query) so the database can return a complete, "
            "summarizable answer."
        )
        return {
            "final_answer": final_answer,
            "messages": [AIMessage(content=final_answer)],
        }

    llm = pick_llm("cheap")
    human_content = (
        f"Original question: {state.user_question}\n\n"
        f"The SQL query that was actually executed:\n{state.generated_sql_query}\n\n"
        f"Raw SQL execution result: {state.sql_query_execution_result}"
    )
    response = llm.invoke(
        [
            ("system", REPRESENT_FINAL_ANSWER_SYSTEM_PROMPT),
            ("human", human_content),
        ]
    )
    final_answer = _extract_text(response.content).strip()

    return {
        "final_answer": final_answer,
        "messages": [AIMessage(content=final_answer)],
    }


def build_sql_analyst_graph():
    """Wire all nodes into a StateGraph using SQLAnalystState, and compile it.

    Graph shape:
        START -> curate_question -> add_context -> generate_sql -> is_safe
        is_safe --(route_after_safety_check)--> execute_sql | cancel_sql
        execute_sql --(route_after_execute_sql)--> generate_sql (retry) | represent_final_answer
        cancel_sql -> END
        represent_final_answer -> END
    """
    graph = StateGraph(SQLAnalystState)

    graph.add_node("curate_question", curate_question)
    graph.add_node("add_context", add_context)
    graph.add_node("generate_sql", generate_sql)
    graph.add_node("is_safe", is_safe)
    graph.add_node("execute_sql", execute_sql)
    graph.add_node("cancel_sql", cancel_sql)
    graph.add_node("represent_final_answer", represent_final_answer)

    graph.add_edge(START, "curate_question")
    graph.add_edge("curate_question", "add_context")
    graph.add_edge("add_context", "generate_sql")
    graph.add_edge("generate_sql", "is_safe")

    graph.add_conditional_edges(
        "is_safe",
        route_after_safety_check,
        {"execute_sql": "execute_sql", "cancel_sql": "cancel_sql"},
    )
    graph.add_conditional_edges(
        "execute_sql",
        route_after_execute_sql,
        {"generate_sql": "generate_sql", "represent_final_answer": "represent_final_answer"},
    )

    graph.add_edge("cancel_sql", END)
    graph.add_edge("represent_final_answer", END)

    return graph.compile()
