"""
Tests for _numeric_fill_advice and the Spec 6 bugfix to _categorical_fill_advice
(utils/data_cleaning.py). No DB, no LLM.

Every DataFrame here uses dtype=str columns deliberately, matching how this
project actually loads CSVs (_read_csv_robust) — the exact condition the
original _categorical_fill_advice numeric check silently failed on.

Run with:
    PYTHONPATH=/Users/dylangagliordi/data-agent uv run python tests/test_numeric_fill_advice.py
"""

import os
import statistics
import sys

import pandas as pd

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.data_cleaning import (
    _categorical_fill_advice,
    _check_missing_values,
    _issue_guidance,
    _numeric_fill_advice,
)


def _numeric_df():
    # dtype=str on purpose — this is exactly how every real column in this
    # project's pipeline is actually loaded.
    values = ["10", "20", "30", "1000", None, "15", "25"]
    return pd.DataFrame({"salary": pd.Series(values, dtype="object")})


def _categorical_df():
    values = ["Finance"] * 6 + ["Tech"] * 2 + [None]
    return pd.DataFrame({"industry": pd.Series(values, dtype="object")})


def test_numeric_column_gets_median_advice_not_categorical():
    df = _numeric_df()
    categorical_advice = _categorical_fill_advice(df, "salary")
    numeric_advice = _numeric_fill_advice(df, "salary")

    assert categorical_advice == "", (
        "a genuinely numeric column must not get categorical (mode/'Unknown') advice — "
        "this is exactly the case the dead pd.api.types.is_numeric_dtype check was missing"
    )
    assert "median" in numeric_advice
    assert "mean" in numeric_advice  # names what NOT to use, and why

    real_median = statistics.median([10.0, 20.0, 30.0, 1000.0, 15.0, 25.0])
    assert f"{real_median:g}" in numeric_advice, (
        f"advice must state the column's REAL median ({real_median}), not a guess"
    )
    print("PASS: a numeric column gets real-median advice, never categorical advice")


def test_categorical_column_still_gets_categorical_advice_no_regression():
    df = _categorical_df()
    categorical_advice = _categorical_fill_advice(df, "industry")
    numeric_advice = _numeric_fill_advice(df, "industry")

    assert numeric_advice == "", "a genuine text column must not get numeric/median advice"
    assert categorical_advice != ""
    assert "Finance" in categorical_advice  # the real dominant mode, named explicitly
    print("PASS: a categorical column still gets mode/'Unknown' advice — no regression")


def test_issue_guidance_end_to_end_for_both_shapes():
    numeric_df = _numeric_df()
    numeric_issues = _check_missing_values(numeric_df)
    assert len(numeric_issues) == 1
    numeric_note = _issue_guidance(numeric_issues[0], df=numeric_df)
    assert "median" in numeric_note

    categorical_df = _categorical_df()
    categorical_issues = _check_missing_values(categorical_df)
    assert len(categorical_issues) == 1
    categorical_note = _issue_guidance(categorical_issues[0], df=categorical_df)
    assert "mode" in categorical_note or "Unknown" in categorical_note

    print("PASS: _issue_guidance end-to-end gives the right advice shape for each real issue")


if __name__ == "__main__":
    test_numeric_column_gets_median_advice_not_categorical()
    test_categorical_column_still_gets_categorical_advice_no_regression()
    test_issue_guidance_end_to_end_for_both_shapes()
    print("\nAll numeric_fill_advice tests passed.")
