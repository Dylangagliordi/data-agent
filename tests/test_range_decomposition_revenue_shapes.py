"""
Spec 1, Part 6: Revenue's harder shapes for numeric range decomposition —
an unbounded minimum ("$10+ billion (USD)"), a mixed-unit closed pair
("$500 million to $1 billion (USD)"), and a bare open maximum
("Less than $1 million (USD)") — not just Salary's simpler "$137K-$171K"
pattern.

BACKGROUND / FINDING (Part 6's other required work — confirming whether
decompose_range_column was ever actually invoked on this dataset):
  grep for "decompose_range_column(" across the codebase finds it called
  ONLY in its own definition and its own test file — NEVER from clean_dataset,
  clean_and_reload, or anywhere else. The salary_min_k/salary_max_k/
  estimate_source columns already present in
  data/data-science-jobs/cleaned/Uncleaned_DS_jobs.csv came from the GENERAL
  composite-field discovery mechanism instead (see cleaning_log.jsonl: a
  resolved "Composite field (discovered): column 'Salary Estimate' ... 100%
  ... '^\\$\\d+K-\\$\\d+K\\s*\\([^)]+\\)$'" entry) — a completely different code
  path (_generate_composite_split_code) than Spec 3's dedicated
  decompose_range_column enrichment flow. decompose_range_column has never
  actually run against this dataset's real data.

Test 1: _detect_range_columns previously only recognized closed "X-Y"/"X to Y"
pairs (_RANGE_PAIR_RE) — an unbounded "$10+ billion" or "Less than $1
million" value was invisible to it. Confirms the fix (_RANGE_OPEN_MIN_RE /
_RANGE_OPEN_MAX_RE) recognizes each shape individually, and a column
containing a realistic MIX of all three shapes now clears
RANGE_DECOMPOSITION_MATCH_THRESHOLD, where it wouldn't have before.

Test 2: live check against the real Revenue column — confirms the fix raises
the real match fraction (52.8% -> 64.3%), but Revenue still does NOT clear
the 0.8 threshold on the real, live data, because ~32% of it is a genuine
"Unknown / Non-Applicable" category value, not a range of any shape — an
honest documented "correctly stayed unflagged" outcome (same discipline as
Part 5's Headquarters finding), not something to force by loosening the
threshold.

Test 3: end-to-end decompose_range_column on a fixture reproducing ALL of
Revenue's real shapes together (closed pair, mixed-unit closed pair,
unbounded minimum, unbounded maximum, and a non-matching placeholder) with a
fake LLM that follows RANGE_DECOMPOSITION_SYSTEM_PROMPT's updated open-ended
guidance: confirms min/max are correctly extracted per shape, the average is
null whenever either bound is genuinely unknown (never a guessed value), and
non-matching/placeholder rows are null across the board.
"""

import shutil
import sys
import tempfile
from contextlib import contextmanager
from io import StringIO
from pathlib import Path

import pandas as pd

import utils.data_cleaning as dc


@contextmanager
def redirect_stdin_yes(count=10):
    original_stdin = sys.stdin
    sys.stdin = StringIO("yes\n" * count)
    try:
        yield
    finally:
        sys.stdin = original_stdin


class _FakeResponse:
    def __init__(self, content):
        self.content = content


print("=" * 70)
print("TEST 1: _detect_range_columns recognizes open-ended shapes")
print("(unbounded minimum, unbounded maximum), not just closed pairs")
print("=" * 70)

open_min_series = pd.Series(["$10+ billion (USD)", "$5+ million (USD)", "2+ billion"])
open_max_series = pd.Series(["Less than $1 million (USD)", "Under $500 thousand", "less than 10 million"])
mixed_unit_pair = pd.Series(["$500 million to $1 billion (USD)"])

assert open_min_series.str.match(dc._RANGE_OPEN_MIN_RE).all(), "unbounded-minimum shapes must all match"
assert open_max_series.str.match(dc._RANGE_OPEN_MAX_RE).all(), "unbounded-maximum shapes must all match"
assert mixed_unit_pair.str.match(dc._RANGE_PAIR_RE).all(), (
    "a closed pair with DIFFERENT scale words on each side (million/billion) must still match"
)
print("PASS: open-ended-minimum, open-ended-maximum, and mixed-unit-pair shapes each match "
      "their respective pattern in isolation.\n")

# A column mixing all of Revenue's real shapes, WITHOUT the "Unknown/Non-Applicable"
# placeholder-like text that legitimately keeps the real column under threshold (see
# Test 2) — proving the detector itself now correctly recognizes this realistic mix.
mixed_df = pd.DataFrame({
    "revenue_like": (
        ["$100 to $500 million (USD)"] * 4
        + ["$10+ billion (USD)"] * 3
        + ["$500 million to $1 billion (USD)"] * 2
        + ["Less than $1 million (USD)"] * 1
    ),
})
before_fix_matches = mixed_df["revenue_like"].str.match(dc._RANGE_PAIR_RE)
after_fix_candidates = dc._detect_range_columns(mixed_df)
assert before_fix_matches.mean() < dc.RANGE_DECOMPOSITION_MATCH_THRESHOLD, (
    f"expected the OLD pair-only pattern to under-count this realistic mix, "
    f"got {before_fix_matches.mean():.1%}"
)
by_col = {c["column"]: c for c in after_fix_candidates}
assert "revenue_like" in by_col, (
    f"expected 'revenue_like' recognized as a range candidate once open-ended shapes "
    f"are counted, got {after_fix_candidates}"
)
assert by_col["revenue_like"]["match_fraction"] == 1.0, by_col["revenue_like"]
print(f"PASS: a realistic mix of closed/mixed-unit/open-min/open-max shapes clears the "
      f"{dc.RANGE_DECOMPOSITION_MATCH_THRESHOLD:.0%} threshold "
      f"({by_col['revenue_like']['match_fraction']:.0%}) once open-ended shapes are recognized "
      f"— the old pair-only pattern would have under-counted it "
      f"({before_fix_matches.mean():.1%}).\n")

print("=" * 70)
print("TEST 2: live check against the real Revenue column — improved, but")
print("still honestly below threshold (documented, not forced)")
print("=" * 70)

real_df = dc._read_csv_robust("data/data-science-jobs/Uncleaned_DS_jobs.csv")
revenue_real = real_df["Revenue"].dropna().astype(str).str.strip()
pair_only = revenue_real.str.match(dc._RANGE_PAIR_RE)
combined = (
    revenue_real.str.match(dc._RANGE_PAIR_RE)
    | revenue_real.str.match(dc._RANGE_OPEN_MIN_RE)
    | revenue_real.str.match(dc._RANGE_OPEN_MAX_RE)
)
assert combined.mean() > pair_only.mean(), (
    f"expected the fix to raise the real match fraction, got {pair_only.mean():.1%} -> {combined.mean():.1%}"
)
assert combined.mean() < dc.RANGE_DECOMPOSITION_MATCH_THRESHOLD, (
    f"Revenue should still legitimately NOT clear the {dc.RANGE_DECOMPOSITION_MATCH_THRESHOLD:.0%} "
    f"threshold on the real data (a real ~32% of it is a genuine 'Unknown / Non-Applicable' "
    f"category, not any range shape) — got {combined.mean():.1%}, which would mean either the "
    f"real data changed or this fix over-matched something it shouldn't have"
)
non_range_placeholder_frac = (revenue_real == "Unknown / Non-Applicable").mean()
real_candidates = dc._detect_range_columns(real_df)
assert not any(c["column"] == "Revenue" for c in real_candidates), (
    f"Revenue must NOT be offered as a candidate on the real, live data, got {real_candidates}"
)
print(f"PASS: real Revenue match fraction improved {pair_only.mean():.1%} -> {combined.mean():.1%} "
      f"after recognizing open-ended shapes, but correctly still below the "
      f"{dc.RANGE_DECOMPOSITION_MATCH_THRESHOLD:.0%} threshold — {non_range_placeholder_frac:.1%} of "
      f"the real column is 'Unknown / Non-Applicable', a genuine non-range category value, not a "
      f"detection miss. Revenue is correctly NOT offered as a decomposition candidate on this "
      f"dataset as it stands today (documented here explicitly, per the same discipline as Part "
      f"5's Headquarters finding, rather than loosening the threshold to force it in).\n")

print("=" * 70)
print("TEST 3: end-to-end decompose_range_column on all of Revenue's real")
print("shapes together — correct min/max per shape, null average when either")
print("bound is genuinely unknown, nulls for non-matching values")
print("=" * 70)


class RevenueShapesLLM:
    """Follows RANGE_DECOMPOSITION_SYSTEM_PROMPT's open-ended guidance: fills
    in the one known bound for an open-ended value, leaves the other bound
    AND the average null — never guesses, never averages a real bound with a
    fabricated one."""

    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        human_text = messages[-1][1]
        assert "Revenue" in human_text
        code = (
            "import pandas as pd\n"
            "import re\n"
            "path = 'PLACEHOLDER'\n"
            "df = pd.read_csv(path, dtype=str)\n"
            "SCALE = {'thousand': 0.001, 'million': 1, 'billion': 1000}\n"
            "def to_millions(num_str, scale_word):\n"
            "    return float(num_str.replace(',', '')) * SCALE.get(scale_word.lower(), 1)\n"
            "def parse(v):\n"
            "    if not isinstance(v, str):\n"
            "        return (None, None)\n"
            "    m = re.match(r'^\\$?([\\d,.]+)\\+\\s*(thousand|million|billion)', v, re.IGNORECASE)\n"
            "    if m:\n"
            "        return (to_millions(m.group(1), m.group(2)), None)\n"
            "    m = re.match(r'^(?:Less than|Under)\\s+\\$?([\\d,.]+)\\s*(thousand|million|billion)', "
            "v, re.IGNORECASE)\n"
            "    if m:\n"
            "        return (None, to_millions(m.group(1), m.group(2)))\n"
            "    m = re.match(r'^\\$?([\\d,.]+)\\s*(thousand|million|billion)?\\s*(?:to|-)\\s*"
            "\\$?([\\d,.]+)\\s*(thousand|million|billion)', v, re.IGNORECASE)\n"
            "    if m:\n"
            "        lo_scale = m.group(2) or m.group(4)\n"
            "        hi_scale = m.group(4) or m.group(2)\n"
            "        return (to_millions(m.group(1), lo_scale), to_millions(m.group(3), hi_scale))\n"
            "    return (None, None)\n"
            "parsed = df['Revenue'].map(parse)\n"
            "df['min_revenue_millions'] = parsed.map(lambda t: t[0])\n"
            "df['max_revenue_millions'] = parsed.map(lambda t: t[1])\n"
            "df['avg_revenue_millions'] = df[['min_revenue_millions', 'max_revenue_millions']]"
            ".mean(axis=1, skipna=False)\n"
            "df.to_csv(path, index=False)\n"
        )
        import re as _re
        m = _re.search(r"exact path\): (.+)", human_text)
        real_path = m.group(1).strip()
        return _FakeResponse(code.replace("PLACEHOLDER", real_path))


revenue_values = [
    "$100 to $500 million (USD)",     # closed pair, same unit
    "$500 million to $1 billion (USD)",  # closed pair, MIXED unit
    "$10+ billion (USD)",             # open minimum
    "Less than $1 million (USD)",     # open maximum
    "Unknown / Non-Applicable",       # non-matching placeholder text
]
job_titles = [f"Role {i}" for i in range(len(revenue_values))]

tmp_dir3 = Path(tempfile.mkdtemp(prefix="range_decompose_revenue_"))
try:
    file_path3 = tmp_dir3 / "jobs.csv"
    df3 = pd.DataFrame({"Revenue": revenue_values, "Job Title": job_titles})
    df3.to_csv(file_path3, index=False)

    candidate3 = {"column": "Revenue", "match_fraction": 0.8, "sample_pattern": revenue_values[0]}
    fake_llm3 = RevenueShapesLLM()

    with redirect_stdin_yes(count=dc.MAX_CLEAN_ATTEMPTS + 2):
        result3 = dc.decompose_range_column(file_path3, candidate3, llm=fake_llm3)

    assert result3["status"] == "resolved", f"expected 'resolved', got {result3}"
    print("result3:", result3)

    out3 = pd.read_csv(file_path3)
    assert out3["Revenue"].tolist() == revenue_values, "the original column must be byte-for-byte unchanged"

    row = out3.iloc[0]  # "$100 to $500 million" -> closed pair, same unit
    assert row["min_revenue_millions"] == 100 and row["max_revenue_millions"] == 500
    assert row["avg_revenue_millions"] == 300

    row = out3.iloc[1]  # "$500 million to $1 billion" -> closed pair, MIXED unit
    assert row["min_revenue_millions"] == 500 and row["max_revenue_millions"] == 1000, row
    assert row["avg_revenue_millions"] == 750, row
    print("PASS: mixed-unit closed pair correctly converted to one consistent unit "
          "(millions) before averaging.\n")

    row = out3.iloc[2]  # "$10+ billion" -> open minimum
    assert row["min_revenue_millions"] == 10000, row
    assert pd.isna(row["max_revenue_millions"]), "unbounded maximum must be null, never guessed"
    assert pd.isna(row["avg_revenue_millions"]), "average must be null when the maximum is unknown"
    print("PASS: unbounded-minimum ('$10+ billion') correctly fills only the known bound; "
          "max and average are null, not guessed.\n")

    row = out3.iloc[3]  # "Less than $1 million" -> open maximum
    assert pd.isna(row["min_revenue_millions"]), "unbounded minimum must be null, never guessed"
    assert row["max_revenue_millions"] == 1, row
    assert pd.isna(row["avg_revenue_millions"]), "average must be null when the minimum is unknown"
    print("PASS: unbounded-maximum ('Less than $1 million') correctly fills only the known bound; "
          "min and average are null, not guessed.\n")

    row = out3.iloc[4]  # "Unknown / Non-Applicable" -> no match at all
    assert pd.isna(row["min_revenue_millions"]) and pd.isna(row["max_revenue_millions"]) and pd.isna(
        row["avg_revenue_millions"]
    ), row
    print("PASS: a non-matching placeholder value is null across all three new columns.\n")
finally:
    shutil.rmtree(tmp_dir3, ignore_errors=True)

print("=" * 70)
print("ALL RANGE-DECOMPOSITION REVENUE-SHAPES (SPEC 1, PART 6) ASSERTIONS PASSED")
print("=" * 70)
