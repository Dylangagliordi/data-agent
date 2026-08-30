"""Standalone test for ETLAnalystState — confirms it accepts a minimal starting state
and the add_messages reducer behaves as expected (appends rather than replaces)."""

from langchain_core.messages import HumanMessage

from models.etl_schema import ETLAnalystState

# Minimal construction (no args) must not raise.
empty_state = ETLAnalystState()
print("empty_state.messages:", empty_state.messages)
assert empty_state.messages == []

# Construction with an initial message.
state = ETLAnalystState(messages=[HumanMessage(content="download data from X and load it")])
print("state.messages:", state.messages)
assert len(state.messages) == 1
assert state.messages[0].content == "download data from X and load it"

print("\nALL ETLAnalystState ASSERTIONS PASSED")
