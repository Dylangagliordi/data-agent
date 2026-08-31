"""
Router sub-agent: classifies an incoming message as either a SQL-analyst-shaped
data question or an ETL-analyst-shaped request, then dispatches to whichever
sub-agent's own compiled graph should handle it.

The router itself has no safety gate of its own — it only classifies and
dispatches; it never executes SQL, downloads anything, or runs generated code.
Both sub-agents it dispatches to already have their own real safety gates
(the SQL analyst's is_safe judge, the ETL analyst's cleaning approval gate),
so nothing here needs to duplicate that.
"""

from langchain_core.messages import HumanMessage

from models.router_schema import DataAgentSchema, RouterSchema
from utils.llm_pick import pick_llm

# Absolute imports, never flat/relative ones — main.py lives at the project
# root and agents/ is a subfolder one level down; a flat `import sql_analyst`
# or `from . import sql_analyst` fails once this module is imported from
# outside the agents/ package (e.g. from main.py at the project root, or from
# a test file under tests/), which is exactly the layout this project runs in.
from agents.sql_analyst import build_sql_analyst_graph
from agents.etl_analyst import build_etl_analyst_graph
from models.schema import SQLAnalystState
from models.etl_schema import ETLAnalystState

# Built once at module load, not once per call — compiling a LangGraph graph
# is real work (building the graph object, validating edges) that doesn't
# need to be repeated on every single node invocation.
_SQL_ANALYST_GRAPH = build_sql_analyst_graph()
_ETL_ANALYST_GRAPH = build_etl_analyst_graph()


ROUTER_SYSTEM_PROMPT = """You classify an incoming request into exactly one of two \
categories, for a data platform with two sub-agents:

- "sql_analyst": the request is a question ABOUT data already loaded into the database \
— counts, aggregates, breakdowns, comparisons, "how many X", "what's the average Y", \
"top N by Z", or any question whose answer comes from querying existing data.
- "etl_analyst": the request is about GETTING or PREPARING data — downloading a file \
from a URL, loading data from an external source, or cleaning/transforming a folder of \
raw data files.

Answer with exactly one of these two literal values, plus a brief comment explaining \
why you classified it that way."""


def router_node(state: DataAgentSchema) -> dict:
    """Classify the last message in state.messages via structured output
    (RouterSchema), and store ONLY the classification and reasoning — never
    invokes either sub-agent itself.
    """
    last_message = state.messages[-1]
    content = last_message.content if hasattr(last_message, "content") else str(last_message)

    llm = pick_llm("cheap").with_structured_output(RouterSchema)
    result = llm.invoke(
        [
            ("system", ROUTER_SYSTEM_PROMPT),
            ("human", content),
        ]
    )
    # result is a real structured Pydantic object here (RouterSchema), not a plain
    # AIMessage — .model_dump() is the correct extraction, .content would be wrong
    # (RouterSchema has no .content attribute at all).
    dumped = result.model_dump()

    return {
        "route_response": dumped["answer"],
        "route_comments": dumped["comments"],
    }


def sql_node(state: DataAgentSchema) -> dict:
    """Dispatch to the compiled SQL analyst graph with the last message's content
    as the user_question — the SQL analyst's own schema defaults handle the rest
    of its internal state.

    Any unexpected exception from the sub-agent is caught here and turned into a
    clear final_answer naming which sub-agent failed and the real error, rather
    than propagating and crashing the router.
    """
    last_message = state.messages[-1]
    content = last_message.content if hasattr(last_message, "content") else str(last_message)

    try:
        result = _SQL_ANALYST_GRAPH.invoke(
            SQLAnalystState(user_question=content),
            config={"recursion_limit": 50},
        )
        final_answer = result["final_answer"]
    except Exception as e:
        final_answer = f"The SQL analyst sub-agent failed with an unexpected error: {type(e).__name__}: {e}"

    return {"final_answer": final_answer}


def etl_node(state: DataAgentSchema) -> dict:
    """Dispatch to the compiled ETL analyst graph with the last message wrapped as
    a HumanMessage — the ETL analyst's own ReAct loop handles the rest.

    Same try/except discipline as sql_node: an unexpected exception is caught and
    turned into a clear final_answer naming the failing sub-agent, never left to
    propagate and crash the router.
    """
    last_message = state.messages[-1]
    content = last_message.content if hasattr(last_message, "content") else str(last_message)

    try:
        result = _ETL_ANALYST_GRAPH.invoke(
            ETLAnalystState(messages=[HumanMessage(content=content)]),
            config={"recursion_limit": 30},
        )
        last_ai_message = result["messages"][-1]
        answer_content = last_ai_message.content
        if isinstance(answer_content, list):
            answer_content = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in answer_content
                if not (isinstance(block, dict) and block.get("type") == "thinking")
            )
        final_answer = answer_content
    except Exception as e:
        final_answer = f"The ETL analyst sub-agent failed with an unexpected error: {type(e).__name__}: {e}"

    return {"final_answer": final_answer}


def router_edge(state: DataAgentSchema) -> str:
    """Conditional edge function for after router_node.

    Returns the plain string classification directly from route_response — this
    return value is passed straight into add_conditional_edges' routing mapping,
    the same discipline as every other conditional edge in this project
    (route_after_safety_check, route_after_execute_sql): never branch by reading
    shared state inside the edge function beyond returning the key itself.
    """
    return state.route_response
