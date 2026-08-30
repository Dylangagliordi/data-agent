"""
Model tiering for the SQL analyst sub-agent.

pick_llm(level) returns a chat model instance for the requested tier:
- "low" / "medium" -> ChatOllama, model "qwen3.5:4b", temperature 0 (local, cheap)
- "high"           -> ChatAnthropic, model "claude-sonnet-5", temperature 0 (best quality)

Only generate_sql uses "high". Every other node call is "low" or "medium" —
chosen deliberately per node, not defaulted uniformly.
"""

import os

from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_ollama import ChatOllama

load_dotenv(os.path.expanduser("~/.hermes/profiles/data-agent/.env"))


def pick_llm(level: str):
    if level in ("low", "medium"):
        # reasoning=False: qwen3.5 defaults to emitting a <think>...</think> block
        # before its real answer. With the default token budget it can burn the
        # entire generation on thinking and return empty content (confirmed via a
        # live call: done_reason="length", eval_count=4058, content=""). Disabling
        # reasoning mode and capping num_predict keeps responses fast and non-empty.
        return ChatOllama(model="qwen3.5:4b", temperature=0, reasoning=False, num_predict=1024)
    elif level == "high":
        # Note: claude-sonnet-5 rejects an explicit `temperature` param
        # ("temperature is deprecated for this model" — confirmed via a live
        # call), so it is omitted here even though low/medium set it to 0.
        return ChatAnthropic(
            model="claude-sonnet-5",
            api_key=os.environ["ANTHROPIC_API_KEY"],
        )
    else:
        raise ValueError(f"Unknown model tier level: {level!r} (expected 'low', 'medium', or 'high')")
