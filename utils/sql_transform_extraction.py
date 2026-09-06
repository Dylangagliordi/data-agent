"""Deterministic extraction of the transformations a generated SQL query
applies that are unique to the specific question being answered — as
opposed to the general, dataset-wide cleaning tracked in cleaning_log.jsonl.

Everything here is derived mechanically from the real SQL text (CTE
structure, computed SELECT expressions, WHERE-clause scoping filters,
GROUP BY, HAVING thresholds, ORDER BY / LIMIT ranking stages). Nothing is
invented or inferred from context outside the query — same discipline as
generate_report.py's `_extract_query_assumptions`.

Shared by utils/generate_report.py and utils/generate_presentation.py so
this (nontrivial) parsing logic has exactly one implementation, rather than
duplicating it — unlike the small, cheap helpers those two modules
deliberately keep as independent copies (see their own docstrings).

Public interface: extract_question_transformations(sql_query: str) -> dict
"""

import re

# Literal values that signal a data-quality placeholder rather than a
# genuine business-scope choice — same list used by generate_report.py's
# cleaning-assumption extraction. Equality filters against these are not
# surfaced here (they belong to the general cleaning story, not this one).
_PLACEHOLDER_LITERALS = {"-1", "n/a", "na", "unknown", "none", "#n/a", "0", ""}

_ARITHMETIC_RE = re.compile(r"[()+\-*/]|\bCASE\b", re.IGNORECASE)
_BARE_COLUMN_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*(\.[a-zA-Z_][a-zA-Z0-9_]*)?$")


def _split_top_level(s: str, sep: str = ",") -> list:
    """Split s on sep, but only at paren-depth 0 (so commas inside a
    function call like AVG((a + b) / 2.0) don't split the expression)."""
    parts = []
    depth = 0
    current = []
    for ch in s:
        if ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p for p in parts if p.strip()]


def _extract_cte_names(sql: str) -> list:
    """Walk a leading WITH clause paren-depth-aware to pull out the real,
    ordered CTE names (e.g. ['base', 'industry_stats', 'top5_highest_paying']).
    Returns [] when the query doesn't start with WITH at all."""
    if not sql:
        return []
    m = re.match(r"\s*WITH\s+", sql, re.IGNORECASE)
    if not m:
        return []
    pos = m.end()
    n = len(sql)
    names = []
    while pos < n:
        while pos < n and sql[pos].isspace():
            pos += 1
        m2 = re.match(r"[a-zA-Z_]\w*", sql[pos:])
        if not m2:
            break
        name = m2.group(0)
        pos += len(name)
        m3 = re.match(r"\s+AS\s*\(", sql[pos:], re.IGNORECASE)
        if not m3:
            break
        names.append(name)
        pos += m3.end()
        depth = 1
        while pos < n and depth > 0:
            if sql[pos] == "(":
                depth += 1
            elif sql[pos] == ")":
                depth -= 1
            pos += 1
        while pos < n and sql[pos].isspace():
            pos += 1
        if pos < n and sql[pos] == ",":
            pos += 1
            continue
        break
    return names


def _extract_computed_columns(sql: str) -> list:
    """Return [{"alias": str, "expression": str}, ...] for every real,
    explicitly-aliased SELECT-list expression (across every CTE and the
    final query) that isn't a bare column passthrough — deduped by alias,
    first occurrence wins, order preserved."""
    if not sql:
        return []
    results = []
    seen_aliases = set()
    for m in re.finditer(r"\bSELECT\b(.+?)\bFROM\b", sql, re.IGNORECASE | re.DOTALL):
        select_list = m.group(1)
        for item in _split_top_level(select_list):
            item = item.strip()
            am = re.match(r"^(.*?)\s+AS\s+([a-zA-Z_]\w*)\s*$", item, re.IGNORECASE | re.DOTALL)
            if not am:
                continue
            expr, alias = am.group(1).strip(), am.group(2).strip()
            if not expr or _BARE_COLUMN_RE.match(expr):
                continue
            if not _ARITHMETIC_RE.search(expr):
                continue
            key = alias.lower()
            if key in seen_aliases:
                continue
            seen_aliases.add(key)
            results.append({"alias": alias, "expression": re.sub(r"\s+", " ", expr)})
    return results


def _extract_grouping_columns(sql: str) -> list:
    """Return a list of GROUP BY column-groups (each a list of column
    names), one per real GROUP BY clause in the query, deduped by identical
    group contents, order preserved."""
    if not sql:
        return []
    groups = []
    seen = set()
    for m in re.finditer(
        r"\bGROUP\s+BY\s+(.+?)(?=\bHAVING\b|\bORDER\s+BY\b|\)|;|$)",
        sql, re.IGNORECASE | re.DOTALL,
    ):
        cols = [c.strip() for c in _split_top_level(m.group(1)) if c.strip()]
        if not cols:
            continue
        key = tuple(c.lower() for c in cols)
        if key in seen:
            continue
        seen.add(key)
        groups.append(cols)
    return groups


def _extract_having_threshold(sql: str) -> "int | None":
    if not sql:
        return None
    m = re.search(
        r"\bHAVING\b.*?\bCOUNT\b\s*\(\s*\*?\s*\)\s*>=?\s*(\d+)",
        sql, re.IGNORECASE | re.DOTALL,
    )
    return int(m.group(1)) if m else None


def _extract_ranking_stages(sql: str) -> list:
    """Return [{"order_by": str, "limit": int|None}, ...] for every real
    ORDER BY clause in the query, in the order they appear — a query that
    ranks-and-caps in an intermediate CTE and then re-sorts in the final
    SELECT produces two stages here, in that order."""
    if not sql:
        return []
    stages = []
    for m in re.finditer(
        r"\bORDER\s+BY\s+(.+?)(?=\)|;|\bGROUP\s+BY\b|$)",
        sql, re.IGNORECASE | re.DOTALL,
    ):
        segment = m.group(1).strip()
        lm = re.match(r"^(.*?)\s+LIMIT\s+(\d+)\s*$", segment, re.IGNORECASE | re.DOTALL)
        if lm:
            order_by, limit = lm.group(1).strip(), int(lm.group(2))
        else:
            order_by, limit = segment, None
        order_by = re.sub(r"\s+", " ", order_by).rstrip(",")
        if order_by:
            stages.append({"order_by": order_by, "limit": limit})
    return stages


def _extract_scope_filters(sql: str) -> list:
    """Return plain-English descriptions of WHERE-clause filters that
    narrow the result to what THIS question is about (ILIKE/LIKE topic
    matches, positive equality/IN filters against real literal values) —
    deliberately excluding IS NULL / IS NOT NULL data-quality checks and
    NOT IN / <> placeholder exclusions, which belong to the general
    cleaning story shown elsewhere, not to this question-specific one."""
    if not sql:
        return []
    filters = []
    seen = set()

    def add(text: str) -> None:
        if text not in seen:
            seen.add(text)
            filters.append(text)

    for col, pattern in re.findall(r"\b(\w+)\s+ILIKE\s+'([^']*)'", sql, re.IGNORECASE):
        add(f"Only rows where {col} matches '{pattern}' (case-insensitive) were included.")

    for col, pattern in re.findall(r"\b(\w+)\s+LIKE\s+'([^']*)'", sql, re.IGNORECASE):
        add(f"Only rows where {col} matches '{pattern}' were included.")

    for col, val in re.findall(r"\b(\w+)\s*=\s*'([^']*)'", sql, re.IGNORECASE):
        if val.lower() in _PLACEHOLDER_LITERALS:
            continue
        add(f"Only rows where {col} = '{val}' were included.")

    for col, is_not, values_str in re.findall(
        r"\b(\w+)\s+(NOT\s+)?IN\s*\(([^)]+)\)", sql, re.IGNORECASE
    ):
        if is_not:
            continue
        values = re.findall(r"'([^']*)'", values_str)
        if values:
            vals_fmt = ", ".join(f"'{v}'" for v in values)
            add(f"Only rows where {col} is one of {vals_fmt} were included.")

    return filters


def extract_question_transformations(sql_query: str) -> dict:
    """Return the full, structured breakdown of question-specific SQL
    shaping for a single query. Every field is independently derived
    mechanically from sql_query's real text; empty/None values mean that
    kind of transformation genuinely wasn't present, not that it was
    skipped."""
    return {
        "cte_steps": _extract_cte_names(sql_query or ""),
        "computed_columns": _extract_computed_columns(sql_query or ""),
        "grouping_columns": _extract_grouping_columns(sql_query or ""),
        "having_threshold": _extract_having_threshold(sql_query or ""),
        "ranking_stages": _extract_ranking_stages(sql_query or ""),
        "scope_filters": _extract_scope_filters(sql_query or ""),
    }


def has_any_transformation(transformations: dict) -> bool:
    """True if extract_question_transformations found anything worth
    showing at all."""
    return bool(
        transformations.get("computed_columns")
        or transformations.get("grouping_columns")
        or transformations.get("having_threshold") is not None
        or transformations.get("ranking_stages")
        or transformations.get("scope_filters")
        or transformations.get("cte_steps")
    )
