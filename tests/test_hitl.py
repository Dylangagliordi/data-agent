"""
Tests for Spec 13, Part 2: Unified HITL Interaction Layer (utils/hitl.py).

Tests the shared primitive directly, plus the acceptance criterion that a
single run touching two different decision types produces two real,
correctly-shaped entries in the SAME unified transcript
(logs/hitl_log.jsonl) — proof the log is genuinely unified, not per-mechanism.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_hitl.py
"""

import contextlib
import io
import json
import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import utils.hitl as hitl_module
from utils.hitl import request_code_approval, request_decision, request_option_choice


@contextlib.contextmanager
def _redirect_stdin(text: str):
    original_stdin = sys.stdin
    sys.stdin = io.StringIO(text)
    try:
        yield
    finally:
        sys.stdin = original_stdin


def _last_log_lines(n: int) -> list:
    lines = [l for l in hitl_module._HITL_LOG_PATH.read_text().splitlines() if l.strip()]
    return [json.loads(l) for l in lines[-n:]]


def test_request_code_approval_yes_and_no():
    with _redirect_stdin("yes\n"):
        approved = request_code_approval("print('hi')", Path("/tmp/fake.csv"), decision_type="_test_code_approval")
    assert approved is True

    with _redirect_stdin("nope\n"):
        approved = request_code_approval("print('hi')", Path("/tmp/fake.csv"), decision_type="_test_code_approval")
    assert approved is False

    entries = _last_log_lines(2)
    assert entries[0]["decision_type"] == "_test_code_approval" and entries[0]["response"] == "yes"
    assert entries[1]["decision_type"] == "_test_code_approval" and entries[1]["response"] == "nope"
    print("PASS: request_code_approval approves on exactly 'yes', declines on anything else, and logs both")


def test_request_code_approval_never_reprompts_on_typo():
    # Same discipline the original _request_approval always had: a typo is a
    # decline, never a re-prompt loop — proven by a single-line stdin with
    # nothing left to read if a second input() were ever attempted.
    with _redirect_stdin("yse\n"):
        approved = request_code_approval("x = 1", Path("/tmp/fake.csv"), decision_type="_test_code_approval")
    assert approved is False
    print("PASS: a typo is treated as a decline with a single read, never a re-prompt")


def test_request_option_choice_valid_first_try():
    options = [{"id": "a", "label": "Option A", "description": "first"}, {"id": "b", "label": "Option B", "description": "second"}]
    with _redirect_stdin("a\n"):
        choice = request_option_choice(options, title="_test menu", decision_type="_test_option_choice")
    assert choice == "a"
    entries = _last_log_lines(1)
    assert entries[0]["decision_type"] == "_test_option_choice" and entries[0]["response"] == "a"
    print("PASS: request_option_choice returns a valid first-try choice and logs it")


def test_request_option_choice_retries_on_invalid():
    options = [{"id": "a", "label": "Option A", "description": ""}, {"id": "b", "label": "Option B", "description": ""}]
    with _redirect_stdin("z\nq\nb\n"):
        choice = request_option_choice(options, title="_test menu", decision_type="_test_option_choice")
    assert choice == "b", "must keep re-prompting until a real option id is given"
    print("PASS: request_option_choice loops past invalid answers until a real option id is chosen")


def test_banner_override_preserves_exact_text():
    # Regression proof for the real bug caught while building this: a
    # mechanically-derived decision_type.upper() banner broke
    # test_transformation_options_live_wiring.py's stdout parsing. banner=
    # must let a caller pin the exact original text.
    buf = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout = buf
    try:
        with _redirect_stdin("a\n"):
            request_decision(
                decision_type="_test_banner_case",
                title="widget",
                context="",
                prompt="pick: ",
                valid_responses={"a"},
                retry_until_valid=True,
                banner="TRANSFORMATION OPTION",
            )
    finally:
        sys.stdout = original_stdout
    printed = buf.getvalue()
    assert "TRANSFORMATION OPTION: widget" in printed
    assert "_TEST_BANNER_CASE" not in printed
    print("PASS: an explicit banner= overrides the mechanically-derived decision_type.upper() header")


def test_unified_log_across_mixed_decision_types():
    """Acceptance criterion: a single 'run' touching two different decision
    types produces two real, correctly-shaped entries in the SAME log."""
    with _redirect_stdin("yes\n"):
        request_code_approval("df.to_csv('x')", Path("/tmp/mixed.csv"), decision_type="_test_mixed_code")
    options = [{"id": "apply", "label": "Apply", "description": ""}, {"id": "skip", "label": "Skip", "description": ""}]
    with _redirect_stdin("apply\n"):
        request_option_choice(options, title="_test mixed menu", decision_type="_test_mixed_choice")

    entries = _last_log_lines(2)
    types_seen = {e["decision_type"] for e in entries}
    assert types_seen == {"_test_mixed_code", "_test_mixed_choice"}, (
        f"expected both real decision types in one unified transcript, got {types_seen}"
    )
    for e in entries:
        assert set(e.keys()) == {"timestamp", "decision_type", "title", "context", "response"}
    print("PASS: two different decision types in one run land as two real, correctly-shaped entries in one unified log")


if __name__ == "__main__":
    test_request_code_approval_yes_and_no()
    test_request_code_approval_never_reprompts_on_typo()
    test_request_option_choice_valid_first_try()
    test_request_option_choice_retries_on_invalid()
    test_banner_override_preserves_exact_text()
    test_unified_log_across_mixed_decision_types()
    print("\nAll hitl tests passed.")
