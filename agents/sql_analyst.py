"""
SQL analyst sub-agent: LangGraph node definitions.
"""

from langchain_core.messages import HumanMessage

from models.schema import JudgeSchema, SQLAnalystState
from utils.db import get_app_reader_connection
from utils.llm_pick import pick_llm

CURATE_QUESTION_SYSTEM_PROMPT = """You clean up the wording of a raw user question about a \
database. Fix grammar, spelling, and phrasing only. Do NOT change what the question is \
actually asking, do NOT add new constraints, and do NOT answer it. Output only the cleaned-up \
question text, nothing else."""


def curate_question(state: SQLAnalystState) -> dict:
    """Node 1: clean up the raw question's wording only (no LLM reasoning about intent).

    Appends the curated question to messages as a HumanMessage.
    """
    llm = pick_llm("low")
    response = llm.invoke(
        [
            ("system", CURATE_QUESTION_SYSTEM_PROMPT),
            ("human", state.user_question),
        ]
    )
    curated = response.content.strip()

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
backticks — just the SQL statement itself."""


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
    sql_query = _strip_sql_formatting(response.content)

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
    llm = pick_llm("medium").with_structured_output(JudgeSchema)
    judgement: JudgeSchema = llm.invoke(
        [
            ("system", IS_SAFE_SYSTEM_PROMPT),
            ("human", state.generated_sql_query),
        ]
    )

    return {"is_safe": judgement.answer, "comments": judgement.comments}
