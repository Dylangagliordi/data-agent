"""
Tests for Spec 12: Scratch Mode (utils/scratch_mode.py).

The safety-gate tests are the core of this suite — each one is a REAL piece
of Python that a cooperative-but-careless generation, or a deliberately
adversarial one, could plausibly produce, checked against the real AST walk,
never a hand-waved "should be fine."

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_scratch_mode.py
"""

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from utils.scratch_mode import check_scratch_code_safety, execute_scratch_code, generate_scratch_code


def test_safe_code_is_accepted():
    code = (
        "import matplotlib.pyplot as plt\n"
        "import statistics\n"
        "median_x = statistics.median(df['x'])\n"
        "fig, ax = plt.subplots()\n"
        "ax.scatter(df['x'], df['y'])\n"
        "ax.axvline(median_x)\n"
        "fig.savefig('/tmp/out.png')\n"
    )
    is_safe, reason = check_scratch_code_safety(code)
    assert is_safe, f"expected safe, got rejected: {reason}"
    print("PASS: real, legitimate scratch code is accepted")


def test_disallowed_import_rejected():
    for code in [
        "import os\nos.system('echo hi')\n",
        "import subprocess\n",
        "from os import system\nsystem('echo hi')\n",
        "import socket\n",
    ]:
        is_safe, reason = check_scratch_code_safety(code)
        assert not is_safe, f"expected rejection for: {code!r}"
        assert "disallowed import" in reason
    print("PASS: imports outside the allowlist are rejected")


def test_disallowed_builtin_calls_rejected():
    for code in [
        "open('/etc/passwd').read()\n",
        "eval('1+1')\n",
        "exec('import os')\n",
        "__import__('os').system('echo hi')\n",
        "getattr(df, 'to_csv')('/tmp/x.csv')\n",
        "globals()\n",
    ]:
        is_safe, reason = check_scratch_code_safety(code)
        assert not is_safe, f"expected rejection for: {code!r}"
        assert "disallowed call" in reason
    print("PASS: dangerous builtins reachable with zero imports are rejected")


def test_dunder_attribute_escape_rejected():
    # The classic sandbox-escape chain: no import, no disallowed builtin call,
    # just attribute traversal up to the base object hierarchy.
    code = "x = ().__class__.__bases__[0].__subclasses__()\n"
    is_safe, reason = check_scratch_code_safety(code)
    assert not is_safe, "the classic __class__/__bases__/__subclasses__ escape must be rejected"
    assert "dunder" in reason
    print("PASS: the classic dunder-attribute introspection escape chain is rejected")


def test_syntax_error_rejected_cleanly():
    is_safe, reason = check_scratch_code_safety("this is not ( valid python")
    assert not is_safe
    assert "syntax error" in reason
    print("PASS: a syntax error in generated code is rejected cleanly, never crashes the check")


def test_generate_scratch_code_prompt_content():
    class FakeLLM:
        def __init__(self):
            self.seen_messages = None

        def invoke(self, messages):
            self.seen_messages = messages
            return SimpleNamespace(content="import matplotlib.pyplot as plt\nplt.savefig('/tmp/x.png')\n")

    fake = FakeLLM()
    code = generate_scratch_code(
        question="highlight the opportunity zone",
        result_data=[{"industry": "Tech", "x": 1, "y": 2}],
        output_path="/tmp/x.png",
        llm=fake,
    )
    assert "import matplotlib.pyplot as plt" in code
    human_content = fake.seen_messages[1][1]
    assert "highlight the opportunity zone" in human_content
    assert "Tech" in human_content
    assert "/tmp/x.png" in human_content
    print("PASS: generate_scratch_code passes the real question, data, and output path to the LLM")


def test_execute_scratch_code_real_success():
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = str(Path(tmp_dir) / "chart.png")
        code = (
            "import matplotlib\n"
            "matplotlib.use('Agg')\n"
            "import matplotlib.pyplot as plt\n"
            "fig, ax = plt.subplots()\n"
            "ax.scatter(df['x'], df['y'])\n"
            f"fig.savefig({out_path!r})\n"
        )
        df = pd.DataFrame({"x": [1, 2, 3], "y": [4, 5, 6]})
        success, error = execute_scratch_code(code, df, out_path)
        assert success, f"expected success, got error: {error}"
        assert Path(out_path).exists() and Path(out_path).stat().st_size > 0
        print("PASS: execute_scratch_code runs real code against the real df and produces a real chart file")


def test_execute_scratch_code_real_failure_never_crashes_caller():
    code = "raise ValueError('deliberate failure')\n"
    df = pd.DataFrame({"x": [1]})
    success, error = execute_scratch_code(code, df, "/tmp/unused.png")
    assert not success
    assert "deliberate failure" in error
    print("PASS: a real execution failure is disclosed via (False, traceback), never crashes the caller")


if __name__ == "__main__":
    test_safe_code_is_accepted()
    test_disallowed_import_rejected()
    test_disallowed_builtin_calls_rejected()
    test_dunder_attribute_escape_rejected()
    test_syntax_error_rejected_cleanly()
    test_generate_scratch_code_prompt_content()
    test_execute_scratch_code_real_success()
    test_execute_scratch_code_real_failure_never_crashes_caller()
    print("\nAll scratch_mode tests passed.")
