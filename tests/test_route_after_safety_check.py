"""Standalone test for the route_after_safety_check conditional edge function."""

from agents.sql_analyst import route_after_safety_check
from models.schema import SQLAnalystState

if __name__ == "__main__":
    safe_state = SQLAnalystState(is_safe="yes")
    print("is_safe='yes' routes to:", route_after_safety_check(safe_state))

    unsafe_state = SQLAnalystState(is_safe="no")
    print("is_safe='no' routes to:", route_after_safety_check(unsafe_state))

    default_state = SQLAnalystState()  # default is_safe = "no"
    print("default state routes to:", route_after_safety_check(default_state))

    assert route_after_safety_check(safe_state) == "execute_sql"
    assert route_after_safety_check(unsafe_state) == "cancel_sql"
    assert route_after_safety_check(default_state) == "cancel_sql"
    print("all assertions passed")
