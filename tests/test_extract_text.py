"""Standalone test for _extract_text — proves it handles both response.content
shapes seen from real LLM calls: a plain string, and a list of content blocks
(thinking block + text block), which is what crashed generate_sql for real.
"""

from agents.sql_analyst import _extract_text

if __name__ == "__main__":
    # Case 1: plain string (the common shape)
    assert _extract_text("SELECT 1;") == "SELECT 1;"
    print("plain string case passed")

    # Case 2: list of content blocks, as actually returned by claude-sonnet-5
    # in extended-thinking mode (reproduced live via tests/debug_generate_sql_crash.py)
    list_content = [
        {"type": "thinking", "thinking": "some reasoning...", "signature": "abc"},
        {"type": "text", "text": "SELECT customer_id FROM olist_customers_dataset;"},
    ]
    result = _extract_text(list_content)
    print("list-of-blocks case ->", repr(result))
    assert result == "SELECT customer_id FROM olist_customers_dataset;"
    print("list-of-blocks case passed")

    # Case 3: list with only a text block (no thinking)
    assert _extract_text([{"type": "text", "text": "hello"}]) == "hello"
    print("text-only-block case passed")

    # Case 4: empty list -> empty string, not a crash
    assert _extract_text([]) == ""
    print("empty list case passed")

    print("\nall _extract_text assertions passed")
