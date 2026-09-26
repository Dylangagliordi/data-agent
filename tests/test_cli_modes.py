"""
Tests for Spec 15, Part 1: Mode-Infrastructure Formalization
(utils/cli_modes.py).

Covers the three real trigger shapes directly (no LLM, no DB needed for
these), the dispatch loop's match/no-match behavior, and proves the registry
is genuinely extensible — a test-only mode is dispatched correctly with zero
changes to dispatch() itself.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_cli_modes.py
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.cli_modes import MODES, Mode, _prepare_trigger, dispatch, exact_match, prefixed


def test_exact_match_trigger():
    trigger = exact_match("map")
    assert trigger("map") == {}
    assert trigger("  Map  ") == {}, "case/whitespace-insensitive, same as the original raw.strip().lower()"
    assert trigger("map: extra") is None
    assert trigger("something else") is None
    print("PASS: exact_match trigger matches case/whitespace-insensitively and only the exact command")


def test_prefixed_trigger():
    trigger = prefixed("profile: ")
    assert trigger("profile: my_table") == {"arg": "my_table"}
    assert trigger("profile:  my_table  ") == {"arg": "my_table"}, "the real remainder is stripped"
    assert trigger("joins") is None
    print("PASS: prefixed trigger extracts and strips the real remainder, and only for its own prefix")


def test_prepare_trigger_two_part_split():
    assert _prepare_trigger("prepare: my_table for a churn model") == {
        "table_name": "my_table",
        "goal": "a churn model",
    }
    assert _prepare_trigger("prepare: my_table for a goal for real") == {
        "table_name": "my_table",
        "goal": "a goal for real",
    }, "splits on the FIRST ' for ' only, matching the original split(' for ', 1)"
    assert _prepare_trigger("prepare: my_table") is None, "no ' for ' at all -> no match"
    assert _prepare_trigger("joins") is None
    print("PASS: the prepare: trigger's two-part split matches the original parsing exactly")


def test_dispatch_finds_first_match_and_calls_handler():
    calls = []

    def _fake_handler(args):
        calls.append(args)

    fake_mode = Mode("_test_fake_mode", exact_match("_test_fake_mode"), _fake_handler)
    MODES.insert(0, fake_mode)
    try:
        matched = dispatch("_test_fake_mode")
        assert matched is True
        assert calls == [{}], "the handler must actually be called with the parsed args"
    finally:
        MODES.remove(fake_mode)
    print("PASS: dispatch() finds a matching mode and calls its real handler")


def test_dispatch_returns_false_when_nothing_matches():
    assert dispatch("how many orders came from São Paulo") is False, (
        "a plain question must not match any registered mode, so main.py knows to "
        "fall through to the normal question path"
    )
    print("PASS: dispatch() returns False for a plain question, letting the caller fall through")


def test_registry_is_genuinely_extensible():
    """Proof this is a real registry, not a cosmetic rename: adding a new
    mode requires touching only MODES, never dispatch() itself."""
    seen = []

    def _fake_handler(args):
        seen.append(args["arg"])

    fake_mode = Mode("_test_extensible_mode", prefixed("_test_extensible: "), _fake_handler)
    MODES.append(fake_mode)
    try:
        assert dispatch("_test_extensible: hello") is True
        assert seen == ["hello"]
    finally:
        MODES.remove(fake_mode)
    print("PASS: a brand-new mode works by only adding a registry entry — dispatch() itself is untouched")


def test_every_registered_mode_name_is_unique():
    names = [m.name for m in MODES]
    assert len(names) == len(set(names)), f"duplicate mode names would make dispatch ambiguous: {names}"
    print(f"PASS: all {len(names)} registered mode names are unique")


if __name__ == "__main__":
    test_exact_match_trigger()
    test_prefixed_trigger()
    test_prepare_trigger_two_part_split()
    test_dispatch_finds_first_match_and_calls_handler()
    test_dispatch_returns_false_when_nothing_matches()
    test_registry_is_genuinely_extensible()
    test_every_registered_mode_name_is_unique()
    print("\nAll cli_modes tests passed.")
