"""
Model tiering for the SQL analyst sub-agent.

pick_llm(level) returns a chat model instance for the requested tier:
- "low" / "medium" -> ChatOllama, model "qwen3.5:4b", temperature 0 (local, free)
- "cheap"          -> ChatAnthropic, model "claude-haiku-4-5", temperature 0 (fast, cheap)
- "high"           -> ChatAnthropic, model "claude-sonnet-5", temperature 0 (best quality)

Only generate_sql uses "high". is_safe and represent_final_answer use "cheap" —
these are the two nodes where a wrong call actually matters (letting an unsafe
query through, or misreporting what happened), so they get a real hosted model
instead of the small local one. curate_question stays on "low" (Ollama) since a
slightly imperfect rewording there is harmless.

COST NOTE: "cheap" and "high" are real Anthropic API calls — they cost real
money per call and require network access, unlike "low"/"medium" which run
fully offline against the free local Ollama server. curate_question remains
the only fully free, fully offline node.
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
    elif level == "cheap":
        # Note: unlike claude-sonnet-5, claude-haiku-4-5 accepts an explicit
        # temperature param without error (confirmed via a live call with
        # temperature=0), so it is passed through here.
        return ChatAnthropic(
            model="claude-haiku-4-5",
            temperature=0,
            api_key=os.environ["ANTHROPIC_API_KEY"],
        )
    elif level == "high":
        # Note: claude-sonnet-5 rejects an explicit `temperature` param
        # ("temperature is deprecated for this model" — confirmed via a live
        # call), so it is omitted here even though low/medium set it to 0.
        return ChatAnthropic(
            model="claude-sonnet-5",
            api_key=os.environ["ANTHROPIC_API_KEY"],
        )
    else:
        raise ValueError(
            f"Unknown model tier level: {level!r} (expected 'low', 'medium', 'cheap', or 'high')"
        )
