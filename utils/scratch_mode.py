"""Scratch Mode (Spec 12): when a visualization question needs a computed or
conditional visual element no fixed chart type can express (a derived
threshold, a quadrant split, a conditional highlight — the real, concrete
case `scripts/build_industry_rating_vs_pay_chart.py` already had to be
hand-written outside the agent to solve), the LLM writes bespoke Python
(pandas + matplotlib only) against the ALREADY-fetched, already-safety-checked
SQL result — never against the live database, never with new data-fetching
ability of its own.

Two-layer safety, the same pattern this project already uses for SQL
(agents/sql_analyst.py's deterministic AST gate as the real boundary, the LLM
judge as a secondary, non-authoritative check): `check_scratch_code_safety`
is a deterministic, AST-based static check of the GENERATED PYTHON — real
boundary, checked before a human ever sees an approval prompt — followed by
the exact same human approval gate `utils.data_cleaning._request_approval`
already implements. Like the SQL AST gate, this is a real, meaningful defense
-in-depth layer against an unexpected/careless generation from a cooperative
model — not a claim of an unbreakable sandbox against a deliberately
adversarial prompt.

Public interface:
    ALLOWED_IMPORTS
    check_scratch_code_safety(code) -> (is_safe, reason)
    generate_scratch_code(question, result_data, output_path, llm) -> str
    execute_scratch_code(code, df, output_path) -> (success, error_str)
"""

import ast
import io
import traceback
from pathlib import Path

import pandas as pd

ALLOWED_IMPORTS = {"pandas", "numpy", "matplotlib", "matplotlib.pyplot", "statistics", "math"}

# Builtins reachable WITHOUT any import — blocking imports alone doesn't stop
# these, since they're always present in Python's default builtins.
_DISALLOWED_CALL_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "input",
    "getattr", "setattr", "delattr", "globals", "locals", "vars",
}


def check_scratch_code_safety(code: str) -> tuple:
    """Parses code with Python's own ast module (never executes it) and
    rejects it if it: imports anything outside ALLOWED_IMPORTS; calls any
    builtin in _DISALLOWED_CALL_NAMES (open/exec/eval/__import__/getattr/...,
    reachable with zero imports); or accesses any dunder attribute (blocks
    the classic `().__class__.__bases__[0].__subclasses__()`-style
    introspection escape, which needs neither an import nor a disallowed
    builtin call).

    Returns (True, "") when safe, (False, reason) otherwise. A syntax error
    in the generated code itself is reported the same way — never crashes
    the caller.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, f"generated code has a syntax error: {e}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS:
                    return False, f"disallowed import: {alias.name!r} (allowed: {sorted(ALLOWED_IMPORTS)})"
        elif isinstance(node, ast.ImportFrom):
            if node.module not in ALLOWED_IMPORTS:
                return False, f"disallowed import: {node.module!r} (allowed: {sorted(ALLOWED_IMPORTS)})"
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _DISALLOWED_CALL_NAMES:
                return False, f"disallowed call: {node.func.id}(...)"
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                return False, f"disallowed dunder attribute access: .{node.attr}"

    return True, ""


def _strip_code_formatting(text: str) -> str:
    """Strip markdown code fences around generated code, if the model added
    them anyway (same pattern as utils/data_cleaning.py's own
    _strip_code_formatting / agents/sql_analyst.py's _strip_sql_formatting —
    kept as its own small local copy here for the same reason those two are:
    this module shouldn't gain a cross-module dependency just for a five-line
    string helper)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


_SCRATCH_MODE_SYSTEM_PROMPT = """You write bespoke Python code for a one-off data \
visualization/analysis need that a fixed chart-type renderer cannot express — a \
computed threshold, a conditional highlight, a derived split, or similar.

Rules, no exceptions:
- Output ONLY raw Python code. No explanation, no commentary, no markdown fences, no backticks.
- The real result data is already loaded for you as a pandas DataFrame named `df`. \
Never fetch, query, or read any other data source — operate only on `df`.
- You may only import from: pandas, numpy, matplotlib, matplotlib.pyplot, statistics, math.
- Never use open(), exec(), eval(), __import__(), compile(), input(), getattr(), \
setattr(), globals(), locals(), or vars().
- Save your finished chart to the EXACT path given to you, via plt.savefig(path) or \
fig.savefig(path). Do not print the DataFrame or return anything — the file you save \
is the only output that matters.
- Write plain, real code only — no placeholders, no TODO comments, no pseudocode."""


def generate_scratch_code(
    question: str, result_data: list, output_path: str, llm, rejection_reason: str = ""
) -> str:
    """One LLM call producing the real code text for this question's bespoke
    visualization, given the real result data and the exact path it must save
    to. Returns the raw code string (never executed here).

    rejection_reason, when non-empty, is the real reason a PREVIOUS attempt
    was rejected (by check_scratch_code_safety, or a real execution failure)
    — included so a retry has a concrete, specific reason to actually change
    its approach rather than plausibly regenerating the same rejected code.
    """
    human_content = (
        f"Question: {question}\n\n"
        f"Real result data (as a list of row dicts — this is exactly what `df` "
        f"will contain, via pd.DataFrame(data)):\n{result_data}\n\n"
        f"Save the finished chart to this exact path: {output_path!r}\n"
    )
    if rejection_reason:
        human_content += (
            f"\nA previous attempt was rejected for this exact reason: {rejection_reason}\n"
            f"Write different code that genuinely avoids this — do not repeat the same approach."
        )
    response = llm.invoke(
        [
            ("system", _SCRATCH_MODE_SYSTEM_PROMPT),
            ("human", human_content),
        ]
    )
    content = response.content
    if isinstance(content, list):
        content = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block) for block in content
        )
    return _strip_code_formatting(content)


def execute_scratch_code(code: str, df: pd.DataFrame, output_path: str) -> tuple:
    """Execute already-approved, already-safety-checked code in a fresh
    namespace with `df` pre-populated as the real result data — the code
    never obtains its own data. Returns (success, error_str), the same shape
    utils.data_cleaning._execute_cleaning_code already uses."""
    namespace = {"__name__": "__scratch__", "df": df}
    try:
        exec(compile(code, "<scratch_mode_code>", "exec"), namespace)
        return True, ""
    except Exception:
        return False, traceback.format_exc()
