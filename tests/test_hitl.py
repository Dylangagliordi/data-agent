"""
Tests for Spec 13, Part 2: Unified HITL Interaction Layer (utils/hitl.py).

No DB, no LLM — pure interaction-logic tests against real piped stdin, with
logs/hitl_log.jsonl redirected to a temp file so this never touches the
project's real transcript.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_hitl.py
"""

import contextlib
import io
import json
import os
import sys
import tempfile
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


@contextlib.contextmanager
def _temp_hitl_log():
    original_path = hitl_module._HITL_LOG_PATH
    with tempfile.TemporaryDirectory() as tmp_dir:
        hitl_module._HITL_LOG_PATH = Path(tmp_dir) / "hitl_log.jsonl"
        try:
            yield hitl_module._HITL_LOG_PATH
        finally:
            hitl_module._HITL_LOG_PATH = original_path


def _read_log_entries(path: Path) -> list:
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_request_decision_single_read_no_reprompt():
    with _temp_hitl_log() as log_path:
        with _redirect_stdin("definitely not yes\n"):
            response = request_decision(
                decision_type="test_decision", title="A test", context="some context", prompt="Answer: "
            )
        assert response == "definitely not yes", "a typo/invalid answer must be returned as-is, never re-prompted"
        entries = _read_log_entries(log_path)
        assert len(entries) == 1
        assert entries[0]["decision_type"] == "test_decision"
        assert entries[0]["response"] == "definitely not yes"
        print("PASS: request_decision (retry_until_valid=False) reads exactly once and logs the real answer")


def test_request_decision_retries_until_valid():
    with _temp_hitl_log() as log_path:
        with _redirect_stdin("bogus\nalso bogus\nb\n"):
            response = request_decision(
                decision_type="test_menu",
                title="Pick one",
                context="",
                prompt="Choose (a/b): ",
                valid_responses={"a", "b"},
                retry_until_valid=True,
            )
        assert response == "b"
        entries = _read_log_entries(log_path)
        assert len(entries) == 1 and entries[0]["response"] == "b", "only the final, valid answer is logged"
        print("PASS: request_decision (retry_until_valid=True) loops past invalid answers and logs only the real, valid one")


def test_request_decision_requires_valid_responses_when_retrying():
    try:
        request_decision(
            decision_type="x", title="x", context="", prompt="x", retry_until_valid=True
        )
        raise AssertionError("expected ValueError when retry_until_valid=True with no valid_responses")
    except ValueError:
        pass
    print("PASS: retry_until_valid=True without valid_responses raises clearly rather than looping forever")


def test_request_code_approval_yes_and_no():
    with _temp_hitl_log() as log_path:
        with _redirect_stdin("yes\n"):
            approved = request_code_approval("print('hi')", "/tmp/fake.csv", decision_type="test_code_approval")
        assert approved is True

        with _redirect_stdin("nope\n"):
            declined = request_code_approval("print('hi')", "/tmp/fake.csv", decision_type="test_code_approval")
        assert declined is False

        entries = _read_log_entries(log_path)
        assert len(entries) == 2
        assert entries[0]["response"] == "yes" and entries[1]["response"] == "nope"
        assert all(e["decision_type"] == "test_code_approval" for e in entries)
        print("PASS: request_code_approval accepts only 'yes', logs both the approval and the decline")


def test_request_option_choice_real_menu():
    with _temp_hitl_log() as log_path:
        options = [
            {"id": "apply", "label": "Apply", "description": "do the thing"},
            {"id": "skip", "label": "Skip", "description": "do nothing"},
        ]
        with _redirect_stdin("bogus\napply\n"):
            chosen = request_option_choice(
                options=options, title="A real menu", context="some context", decision_type="test_option"
            )
        assert chosen == "apply"
        entries = _read_log_entries(log_path)
        assert len(entries) == 1 and entries[0]["response"] == "apply"
        print("PASS: request_option_choice loops past an invalid id and returns/logs the real chosen option")


def test_unified_transcript_across_mixed_decision_types():
    """Acceptance criterion 2: a run touching two different decision types
    produces two real, correctly-shaped entries in the SAME unified log."""
    with _temp_hitl_log() as log_path:
        with _redirect_stdin("yes\n"):
            request_code_approval("code", "/tmp/f.csv", decision_type="cleaning_code_approval")
        with _redirect_stdin("skip\n"):
            request_option_choice(
                options=[{"id": "skip", "label": "Skip", "description": ""}],
                title="A transformation choice",
                decision_type="transformation_option_choice",
            )
        entries = _read_log_entries(log_path)
        assert len(entries) == 2
        decision_types = {e["decision_type"] for e in entries}
        assert decision_types == {"cleaning_code_approval", "transformation_option_choice"}
        print("PASS: a mixed-decision-type run produces one real, unified transcript across both mechanisms")


if __name__ == "__main__":
    test_request_decision_single_read_no_reprompt()
    test_request_decision_retries_until_valid()
    test_request_decision_requires_valid_responses_when_retrying()
    test_request_code_approval_yes_and_no()
    test_request_option_choice_real_menu()
    test_unified_transcript_across_mixed_decision_types()
    print("\nAll hitl tests passed.")
