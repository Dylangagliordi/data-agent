"""
Tests for Spec 4: batching fail/warn-level issues that require identical
treatment across columns.

check_rubric()'s checks run per-column, so the same underlying problem in
several columns (e.g. "-1" placeholder tokens in eight different columns)
produces eight separate issue strings, each processed with its own LLM call and
approval prompt — even when the correct fix is identical logic applied to a
different column name. This spec batches issues together ONLY when a
mechanically-verified treatment signature proves their required fix is
genuinely identical (never an LLM's guess that two issues "seem similar").

Test 1: signature correctness for all six defined categories — two issues that
should match produce equal signatures; a near-but-not-identical variant
produces a different signature (or None).

Test 2: grouping correctness — 3 placeholder issues sharing {'-1'} plus one
with {'-1', 'unknown'} produce one group of 3 and one group of 1.

Test 3: end-to-end batch fix (the original motivating bug) — 8 columns all
needing the identical "-1" placeholder fix get exactly ONE approval prompt, are
all correctly fixed, and the result lands in fail_batch_records (not 8 separate
IssueCleaningRecords).

Test 4: mixed-category file — one placeholder batch (3 columns), one currency/
unit batch (2 columns), and one missing-values issue (no signature): the
placeholder and currency groups each get their own combined cycle, and the
missing-values issue is processed on the (unaffected) warn-level path.

Test 5: warn-level plural batches — warn issues split across two distinct
Tier 1/2 signatures (currency/unit and boolean-families) produce two separate
WarnBatchRecords, never one combined batch across mismatched signatures.

Test 6: disclosure accuracy — unresolved_issues_for_record correctly reports
resolved/unresolved status for issues that were part of a batch (fail or warn).

Test 7: report rendering — generate_report's cleaning section renders one
combined block per batch (listing all covered columns) and correctly handles
multiple warn batches in the same file.
"""

import re
import shutil
import sys
import tempfile
from contextlib import contextmanager, redirect_stdout
from io import StringIO
from pathlib import Path

import pandas as pd

import utils.data_cleaning as dc
import utils.generate_report as gr


@contextmanager
def redirect_stdin_yes(count=20):
    original_stdin = sys.stdin
    sys.stdin = StringIO("yes\n" * count)
    try:
        yield
    finally:
        sys.stdin = original_stdin


class _FakeResponse:
    def __init__(self, content):
        self.content = content


def _real_path(human_text: str) -> str:
    match = re.search(r"exact path\): (.+)", human_text)
    return match.group(1).strip()


def count_approval_prompts(captured_stdout: str) -> int:
    return len(re.findall(r"GENERATED CLEANING CODE for:", captured_stdout))


print("=" * 70)
print("TEST 1: signature correctness for all six defined categories")
print("=" * 70)

# 1a. Placeholder values: identical token set -> equal signature; one extra
# token -> different signature.
issue_a = (
    "Placeholder values: column 'a' has 1 value(s) that look like placeholders "
    "standing in for real data (['-1']), mixed in among otherwise genuine values."
)
issue_b = (
    "Placeholder values: column 'b' has 1 value(s) that look like placeholders "
    "standing in for real data (['-1']), mixed in among otherwise genuine values."
)
issue_c = (
    "Placeholder values: column 'c' has 2 value(s) that look like placeholders "
    "standing in for real data (['-1', 'unknown']), mixed in among otherwise genuine values."
)
sig_a, sig_b, sig_c = (dc._issue_treatment_signature(i) for i in (issue_a, issue_b, issue_c))
assert sig_a == sig_b and sig_a is not None, f"expected equal, non-None signatures, got {sig_a}, {sig_b}"
assert sig_a != sig_c, f"an extra token must produce a different signature, got {sig_a} == {sig_c}"
print("PASS: placeholder — identical token sets match, an extra token does not.\n")

# 1b. Currency/unit symbols: identical symbol set -> equal; different symbol -> different.
df_currency = pd.DataFrame({
    "price": ["$50.00", "$60.00", "45.00"],
    "cost": ["$10.00", "$20.00", "30.00"],
    "weight": ["50kg", "60kg", "70"],
})
cur_issues = dc._check_currency_unit_symbols(df_currency)
by_col = {re.search(r"column '([^']+)'", i).group(1): i for i in cur_issues}
sig_price = dc._issue_treatment_signature(by_col["price"])
sig_cost = dc._issue_treatment_signature(by_col["cost"])
sig_weight = dc._issue_treatment_signature(by_col["weight"])
assert sig_price == sig_cost and sig_price is not None
assert sig_price != sig_weight, "a currency symbol and a unit suffix must not share a signature"
print("PASS: currency/unit — identical symbol sets match, a different symbol/suffix does not.\n")

# 1c. Spreadsheet artifacts: formula vs excel_error never share a signature.
df_sheet = pd.DataFrame({
    "f1": ["=SUM(A1:A2)", "x", "y"],
    "f2": ["=AVERAGE(B1:B2)", "x", "y"],
    "e1": ["#DIV/0!", "x", "y"],
})
sheet_issues = dc._check_spreadsheet_artifacts(df_sheet)
by_col_sheet = {re.search(r"column '([^']+)'", i).group(1): i for i in sheet_issues}
sig_f1 = dc._issue_treatment_signature(by_col_sheet["f1"])
sig_f2 = dc._issue_treatment_signature(by_col_sheet["f2"])
sig_e1 = dc._issue_treatment_signature(by_col_sheet["e1"])
assert sig_f1 == sig_f2 and sig_f1 == ("spreadsheet_artifact", "formula")
assert sig_e1 == ("spreadsheet_artifact", "excel_error")
assert sig_f1 != sig_e1, "a formula issue and an excel-error issue must never share a signature"
print("PASS: spreadsheet artifacts — two formula columns match, formula vs excel_error do not.\n")

# 1d. Locale-specific number formatting: the (fixed) US/EU pair.
df_locale = pd.DataFrame({
    "a": ["1,234.56", "1.234,56", "100"],
    "b": ["9,876.54", "9.876,54", "200"],
})
locale_issues = dc._check_locale_number_formatting(df_locale)
sig_locale_a = dc._issue_treatment_signature(locale_issues[0])
sig_locale_b = dc._issue_treatment_signature(locale_issues[1])
assert sig_locale_a == sig_locale_b == ("locale_format", frozenset({"US-style", "EU-style"}))
print("PASS: locale formatting — both columns share the fixed US/EU signature.\n")

# 1e. Inconsistent boolean representations: identical family pair -> equal;
# a column mixing a DIFFERENT pair of families -> different signature.
df_bool = pd.DataFrame({
    "b1": ["y", "n", "1", "0"],
    "b2": ["y", "n", "1", "0"],
    "b3": ["true", "false", "1", "0"],
})
bool_issues = dc._check_boolean_inconsistency(df_bool)
by_col_bool = {re.search(r"column '([^']+)'", i).group(1): i for i in bool_issues}
sig_b1 = dc._issue_treatment_signature(by_col_bool["b1"])
sig_b2 = dc._issue_treatment_signature(by_col_bool["b2"])
sig_b3 = dc._issue_treatment_signature(by_col_bool["b3"])
assert sig_b1 == sig_b2 and sig_b1 == ("boolean_families", frozenset({"y/n", "1/0"}))
assert sig_b1 != sig_b3, "a different pair of conflicting families must produce a different signature"
print("PASS: boolean families — identical conflicting pairs match, a different pair does not.\n")

# 1f. Lost leading zeros: identical target length -> equal; a different length -> different.
df_zip = pd.DataFrame({
    "zip1": ["02139", "2139", "30301", "30302"],
    "zip2": ["04321", "4321", "50505", "50506"],
    "postal3": ["001234", "01234", "567890", "567891"],
})
zip_issues = dc._check_lost_leading_zeros(df_zip)
by_col_zip = {re.search(r"column '([^']+)'", i).group(1): i for i in zip_issues}
sig_zip1 = dc._issue_treatment_signature(by_col_zip["zip1"])
sig_zip2 = dc._issue_treatment_signature(by_col_zip["zip2"])
sig_postal3 = dc._issue_treatment_signature(by_col_zip["postal3"])
assert sig_zip1 == sig_zip2 == ("leading_zeros", 5)
assert sig_postal3 == ("leading_zeros", 6)
assert sig_zip1 != sig_postal3, "a different common target length must produce a different signature"
print("PASS: lost leading zeros — identical target lengths match, a different length does not.\n")

print("=" * 70)
print("TEST 2: grouping correctness")
print("=" * 70)

group_issues = [issue_a, issue_b, issue_c,
                "Placeholder values: column 'd' has 1 value(s) that look like placeholders "
                "standing in for real data (['-1']), mixed in among otherwise genuine values."]
groups = dc._group_issues_by_signature(group_issues)
print("groups:", groups)
sizes = sorted(len(g) for g in groups)
assert sizes == [1, 3], f"expected one group of 3 (shared {{'-1'}}) and one of 1 (odd {{'-1','unknown'}}), got sizes {sizes}"
big_group = next(g for g in groups if len(g) == 3)
assert set(big_group) == {issue_a, issue_b, group_issues[3]}
print("PASS: 3 issues sharing {'-1'} grouped together; the {'-1','unknown'} issue stands alone.\n")


print("=" * 70)
print("TEST 3: end-to-end batch fix — the original motivating bug")
print("=" * 70)

N_COLS = 8
N_ROWS = 10


class PlaceholderBatchLLM:
    """A real, working fix for the -1 placeholder shared across all 8 columns —
    mirrors the reference notebook's single-line
    df[categorical_cols] = df[categorical_cols].replace('-1', 'na')."""

    def __init__(self):
        self.call_count = 0

    def invoke(self, messages):
        self.call_count += 1
        human_text = messages[-1][1]
        path = _real_path(human_text)
        cols = [f"col_{i}" for i in range(N_COLS)]
        code = (
            "import pandas as pd\n"
            f"path = {path!r}\n"
            "df = pd.read_csv(path, dtype=str)\n"
            f"cols = {cols!r}\n"
            "df[cols] = df[cols].replace('-1', 'na')\n"
            "df.to_csv(path, index=False)\n"
        )
        return _FakeResponse(code)


tmp_dir3 = Path(tempfile.mkdtemp(prefix="batching_placeholder_"))
try:
    file_path3 = tmp_dir3 / "data.csv"
    data3 = {}
    for i in range(N_COLS):
        col_vals = [f"val_{i}_{j}" for j in range(N_ROWS)]
        col_vals[0] = "-1"  # exactly one placeholder per column -> < 50% minority
        data3[f"col_{i}"] = col_vals
    pd.DataFrame(data3).to_csv(file_path3, index=False)

    static_issues3 = dc.check_rubric(file_path3)
    assert len(static_issues3) == N_COLS, f"expected {N_COLS} placeholder issues, got {static_issues3}"
    assert all(i.startswith("Placeholder values:") for i in static_issues3)

    fake_llm3 = PlaceholderBatchLLM()
    stdout_capture = StringIO()
    with redirect_stdout(stdout_capture):
        with redirect_stdin_yes():
            result3 = dc.clean_dataset(str(tmp_dir3), llm=fake_llm3)
    captured3 = stdout_capture.getvalue()
    print(result3.summary())

    prompt_count3 = count_approval_prompts(captured3)
    assert prompt_count3 == 1, (
        f"expected exactly ONE approval prompt for all {N_COLS} identical placeholder "
        f"issues batched together, got {prompt_count3}"
    )
    assert fake_llm3.call_count == 1, f"expected exactly 1 LLM call, got {fake_llm3.call_count}"
    print(f"PASS: exactly {prompt_count3} approval prompt for {N_COLS} identical-treatment columns "
          f"(would have been {N_COLS} before this spec).\n")

    assert len(result3.cleaned_files) == 1, f"expected 1 cleaned file, got {result3.summary()}"
    rec3 = result3.cleaned_files[0]
    assert rec3.fail_issue_records == [], (
        f"all 8 issues should be in ONE fail_batch_record, not individual "
        f"fail_issue_records, got {rec3.fail_issue_records}"
    )
    assert len(rec3.fail_batch_records) == 1, f"expected exactly 1 batch, got {rec3.fail_batch_records}"
    batch3 = rec3.fail_batch_records[0]
    assert len(batch3.issues) == N_COLS
    assert batch3.status == "resolved", f"expected the batch resolved, got {batch3}"
    assert batch3.signature == ("placeholder", frozenset({"-1"}), "text")
    print("PASS: all 8 issues landed in exactly ONE FailBatchRecord, resolved.\n")

    result_df3 = pd.read_csv(tmp_dir3 / "cleaned" / "data.csv", dtype=str)
    for i in range(N_COLS):
        assert "-1" not in result_df3[f"col_{i}"].tolist(), (
            f"col_{i} still contains the placeholder after the batched fix"
        )
    print("PASS: all 8 columns correctly fixed by the single batched cycle.\n")
finally:
    shutil.rmtree(tmp_dir3, ignore_errors=True)


print("=" * 70)
print("TEST 4: mixed-category file — placeholder batch + currency batch +")
print("one unaffected missing-values issue")
print("=" * 70)


class MixedCategoryLLM:
    def __init__(self):
        self.calls = []

    def invoke(self, messages):
        human_text = messages[-1][1]
        self.calls.append(human_text)
        path = _real_path(human_text)

        if "Placeholder values" in human_text:
            cols = ["ph_0", "ph_1", "ph_2"]
            code = (
                "import pandas as pd\n"
                f"path = {path!r}\n"
                "df = pd.read_csv(path, dtype=str)\n"
                f"cols = {cols!r}\n"
                "df[cols] = df[cols].replace('-1', 'na')\n"
                "df.to_csv(path, index=False)\n"
            )
            return _FakeResponse(code)

        if "Currency/unit symbols" in human_text:
            cols = ["price_0", "price_1"]
            code = (
                "import pandas as pd\n"
                f"path = {path!r}\n"
                "df = pd.read_csv(path, dtype=str)\n"
                f"cols = {cols!r}\n"
                "for c in cols:\n"
                "    df[c] = df[c].str.replace('$', '', regex=False)\n"
                "df.to_csv(path, index=False)\n"
            )
            return _FakeResponse(code)

        if "Missing values" in human_text:
            code = (
                "import pandas as pd\n"
                f"path = {path!r}\n"
                "df = pd.read_csv(path, dtype=str)\n"
                "df['amount'] = df['amount'].fillna('0')\n"
                "df.to_csv(path, index=False)\n"
            )
            return _FakeResponse(code)

        raise AssertionError(f"unexpected call: {human_text[:200]!r}")


tmp_dir4 = Path(tempfile.mkdtemp(prefix="batching_mixed_"))
try:
    file_path4 = tmp_dir4 / "data.csv"
    n = 10
    data4 = {
        "ph_0": ["-1"] + [f"a{j}" for j in range(1, n)],
        "ph_1": ["-1"] + [f"b{j}" for j in range(1, n)],
        "ph_2": ["-1"] + [f"c{j}" for j in range(1, n)],
        # All $-prefixed (not a mix of bare-numeric and $-prefixed) so this trips
        # ONLY "Currency/unit symbols", not also "Wrong data type" (which fires when
        # a column is MOSTLY bare-numeric with a few non-numeric values mixed in —
        # here every value fails numeric coercion because of the $, so that check's
        # own ratio condition never activates).
        "price_0": [f"${j}.00" for j in range(1, n + 1)],
        "price_1": [f"${j + 100}.00" for j in range(1, n + 1)],
        # ~10% missing, WARN-level, no defined signature -> stays its own leftover batch.
        "amount": [None] + [str(10 + j) for j in range(1, n)],
    }
    pd.DataFrame(data4).to_csv(file_path4, index=False)

    static_issues4 = dc.check_rubric(file_path4)
    print("static issues:", static_issues4)
    fail4, warn4 = dc._split_issues_by_severity(static_issues4)
    # Placeholder values is fail-level; Currency/unit symbols and Missing values are
    # both warn-level — so this fixture exercises grouping on BOTH severity levels at
    # once: a fail-level batch (placeholder x3) and, on the warn side, a real batch
    # (currency x2) alongside one genuinely unrelated, unaffected issue (missing
    # values, no defined signature).
    assert len(fail4) == 3 and all(i.startswith("Placeholder values:") for i in fail4), (
        f"expected exactly 3 placeholder fail issues, got {fail4}"
    )
    assert len(warn4) == 3, f"expected 2 currency + 1 missing-values warn issues, got {warn4}"
    assert sum(1 for i in warn4 if i.startswith("Currency/unit symbols:")) == 2
    assert sum(1 for i in warn4 if i.startswith("Missing values:")) == 1

    fake_llm4 = MixedCategoryLLM()
    with redirect_stdin_yes():
        result4 = dc.clean_dataset(str(tmp_dir4), llm=fake_llm4)
    print(result4.summary())

    all_recs4 = result4.cleaned_files + result4.skipped_files
    assert len(all_recs4) == 1
    rec4 = all_recs4[0]

    assert len(rec4.fail_batch_records) == 1, (
        f"expected exactly 1 fail-level batch (placeholder x3), got {rec4.fail_batch_records}"
    )
    assert len(rec4.fail_batch_records[0].issues) == 3
    assert rec4.fail_issue_records == [], (
        f"the only fail-level issue category here (placeholder) has a signature, so "
        f"nothing should be ungrouped, got {rec4.fail_issue_records}"
    )
    print("PASS: the 3 placeholder issues got their own combined fail-level cycle.\n")

    assert len(rec4.warn_batches) == 2, (
        f"expected 2 warn batches: the currency pair (real signature match) and the "
        f"lone missing-values issue (pooled leftover), got {rec4.warn_batches}"
    )
    warn_sizes4 = sorted(len(b.issues) for b in rec4.warn_batches)
    assert warn_sizes4 == [1, 2], f"expected warn batch sizes [1, 2], got {warn_sizes4}"
    currency_batch = next(b for b in rec4.warn_batches if len(b.issues) == 2)
    missing_batch = next(b for b in rec4.warn_batches if len(b.issues) == 1)
    assert all(i.startswith("Currency/unit symbols:") for i in currency_batch.issues)
    assert missing_batch.issues[0].startswith("Missing values:")
    print("PASS: the 2 currency issues got their own combined warn-level cycle, while the "
          "unrelated missing-values issue (no signature) was processed on its own, unaffected.\n")
finally:
    shutil.rmtree(tmp_dir4, ignore_errors=True)


print("=" * 70)
print("TEST 5: warn-level plural batches — two distinct Tier 1/2 signatures")
print("produce two separate WarnBatchRecords")
print("=" * 70)


class WarnPluralBatchLLM:
    def invoke(self, messages):
        human_text = messages[-1][1]
        path = _real_path(human_text)
        if "Currency/unit symbols" in human_text:
            cols = ["cur_0", "cur_1"]
            code = (
                "import pandas as pd\n"
                f"path = {path!r}\n"
                "df = pd.read_csv(path, dtype=str)\n"
                f"cols = {cols!r}\n"
                "for c in cols:\n"
                "    df[c] = df[c].str.replace('$', '', regex=False)\n"
                "df.to_csv(path, index=False)\n"
            )
        elif "Inconsistent boolean representations" in human_text:
            cols = ["bool_0", "bool_1"]
            mapping = {"y": "true", "n": "false", "1": "true", "0": "false"}
            code = (
                "import pandas as pd\n"
                f"path = {path!r}\n"
                "df = pd.read_csv(path, dtype=str)\n"
                f"cols = {cols!r}\n"
                f"mapping = {mapping!r}\n"
                "for c in cols:\n"
                "    df[c] = df[c].str.lower().map(mapping)\n"
                "df.to_csv(path, index=False)\n"
            )
        else:
            raise AssertionError(f"unexpected call: {human_text[:200]!r}")
        return _FakeResponse(code)


tmp_dir5 = Path(tempfile.mkdtemp(prefix="batching_warn_plural_"))
try:
    file_path5 = tmp_dir5 / "data.csv"
    data5 = {
        "cur_0": ["$5.00", "$6.00", "7.00"],
        "cur_1": ["$8.00", "$9.00", "10.00"],
        "bool_0": ["y", "n", "1"],
        "bool_1": ["y", "n", "0"],
    }
    pd.DataFrame(data5).to_csv(file_path5, index=False)

    static_issues5 = dc.check_rubric(file_path5)
    fail5, warn5 = dc._split_issues_by_severity(static_issues5)
    assert fail5 == [], f"expected no fail-level issues in this fixture, got {fail5}"
    assert len(warn5) == 4, f"expected 4 warn-level issues (2 currency + 2 boolean), got {warn5}"

    fake_llm5 = WarnPluralBatchLLM()
    with redirect_stdin_yes():
        result5 = dc.clean_dataset(str(tmp_dir5), llm=fake_llm5)
    print(result5.summary())

    all_recs5 = result5.cleaned_files + result5.skipped_files
    assert len(all_recs5) == 1
    rec5 = all_recs5[0]
    print("warn_batches:", [(len(b.issues), b.status) for b in rec5.warn_batches])

    assert len(rec5.warn_batches) == 2, (
        f"expected 2 separate WarnBatchRecords (currency, boolean) — never combined "
        f"across mismatched signatures, got {rec5.warn_batches}"
    )
    sizes5 = sorted(len(b.issues) for b in rec5.warn_batches)
    assert sizes5 == [2, 2], f"expected two batches of 2 each, got {sizes5}"
    for b in rec5.warn_batches:
        assert b.status == "resolved", f"expected each warn batch resolved, got {b}"
    print("PASS: two distinct warn-level signatures produced two separate WarnBatchRecords, "
          "never one combined batch across mismatched signatures.\n")
finally:
    shutil.rmtree(tmp_dir5, ignore_errors=True)


print("=" * 70)
print("TEST 6: disclosure accuracy — unresolved_issues_for_record with batches")
print("=" * 70)

fail_batch_resolved = dc.FailBatchRecord(
    issues=[issue_a, issue_b], signature=("placeholder", frozenset({"-1"})), status="resolved",
)
warn_issue_x = "Currency/unit symbols: column 'x' has 1 value(s) ... (['$'])."
warn_issue_y = "Currency/unit symbols: column 'y' has 1 value(s) ... (['$'])."
warn_batch_unresolved = dc.WarnBatchRecord(issues=[warn_issue_x, warn_issue_y], status="skipped_failed")

standalone_issue = "Invalid values: column 'z' has 1 negative value(s)."
standalone_rec = dc.IssueCleaningRecord(issue=standalone_issue, status="resolved")

rec6 = dc.FileCleaningRecord(
    file_name="whatever.csv",
    issues=[issue_a, issue_b, warn_issue_x, warn_issue_y, standalone_issue],
    fail_issue_records=[standalone_rec],
    fail_batch_records=[fail_batch_resolved],
    warn_batches=[warn_batch_unresolved],
    status="skipped_declined",
)
unresolved6 = dc.unresolved_issues_for_record(rec6)
print("unresolved:", unresolved6)
assert issue_a not in unresolved6 and issue_b not in unresolved6, (
    "issues in a RESOLVED fail batch must count as resolved"
)
assert standalone_issue not in unresolved6, "a resolved standalone issue must count as resolved"
assert warn_issue_x in unresolved6 and warn_issue_y in unresolved6, (
    "issues in an UNRESOLVED (skipped_failed) warn batch must still count as unresolved"
)
print("PASS: unresolved_issues_for_record correctly reflects both a resolved fail batch "
      "and an unresolved warn batch.\n")


print("=" * 70)
print("TEST 7: report rendering — one combined block per batch,")
print("multiple warn batches handled correctly")
print("=" * 70)

file_rec7 = {
    "status": "cleaned",
    "row_count_before": 10,
    "row_count_after": 10,
    "row_loss_flagged": False,
    "issues_found": [
        {"issue": issue_a, "severity": "fail"},
        {"issue": issue_b, "severity": "fail"},
        {"issue": warn_issue_x, "severity": "warn"},
        {"issue": warn_issue_y, "severity": "warn"},
    ],
    "issues_resolved": [issue_a, issue_b, warn_issue_x, warn_issue_y],
    "issues_still_unresolved": [],
    "fail_issues": [],
    "fail_batches": [
        {
            "issues": [issue_a, issue_b],
            "signature": ["placeholder", ["-1"]],
            "status": "resolved",
            "reasoning_comments": ["# Replaced the -1 placeholder with a real null across both columns."],
        }
    ],
    "warn_batches": [
        {
            "issues": [warn_issue_x],
            "status": "resolved",
            "reasoning_comments": ["# Stripped the $ symbol from column x."],
        },
        {
            "issues": [warn_issue_y],
            "status": "resolved",
            "reasoning_comments": ["# Stripped the $ symbol from column y."],
        },
    ],
}
entry_meta7 = {"trigger": "manual", "timestamp": "2026-01-01T00:00:00+00:00"}
html7 = gr._section_data_cleaning({"my_table": (entry_meta7, file_rec7)})
print(html7[:2000])

assert "identical fix applied across 2 columns" in html7, (
    "expected ONE combined block naming the batch size, not 2 separate near-identical blocks"
)
assert gr._esc(issue_a) in html7 and gr._esc(issue_b) in html7, (
    "both batched issues must be listed inside the combined block"
)
assert html7.count("Replaced the -1 placeholder") == 1, (
    "the batch's shared reasoning must be shown exactly ONCE, not once per issue"
)
assert "Stripped the $ symbol from column x" in html7 and "Stripped the $ symbol from column y" in html7, (
    "both warn batches' own reasoning must be rendered"
)
print("PASS: report renders one combined block per fail batch (shared reasoning shown once) "
      "and correctly handles multiple warn batches.\n")

print("=" * 70)
print("TEST 8: dtype-aware signature (Part 4 safety fix) — a numeric column")
print("and a text column sharing the same placeholder tokens must NOT batch")
print("=" * 70)

# Reproduces the real observed bug: 'Rating' (numeric) and 'Headquarters'
# (text) each had a minority '-1' placeholder and were batched together under
# one {'-1'} signature purely because the tokens matched, with no check that
# the columns held the same general kind of value. 'City' is a second text
# column with the same placeholder, added so the surviving text-side group
# still has something real to batch.
df_mixed_dtype = pd.DataFrame({
    "rating": ["4.1", "3.9", "-1", "4.5", "3.2"],
    "headquarters": ["New York, NY", "-1", "Boston, MA", "Austin, TX", "Denver, CO"],
    "city": ["Chicago", "Miami", "-1", "Seattle", "Dallas"],
})
mixed_issues = dc._check_placeholder_values(df_mixed_dtype)
by_col_mixed = {re.search(r"column '([^']+)'", i).group(1): i for i in mixed_issues}
assert set(by_col_mixed) == {"rating", "headquarters", "city"}

# Without a df, signatures are token-only (pre-existing behavior) and all three
# still match — this is the exact hole Part 4 closes.
sig_rating_no_df = dc._issue_treatment_signature(by_col_mixed["rating"])
sig_hq_no_df = dc._issue_treatment_signature(by_col_mixed["headquarters"])
assert sig_rating_no_df == sig_hq_no_df == ("placeholder", frozenset({"-1"}))

# With the real df, the numeric 'rating' column and the text 'headquarters'/
# 'city' columns must diverge by dtype category even though the token set is
# identical.
sig_rating = dc._issue_treatment_signature(by_col_mixed["rating"], df=df_mixed_dtype)
sig_hq = dc._issue_treatment_signature(by_col_mixed["headquarters"], df=df_mixed_dtype)
sig_city = dc._issue_treatment_signature(by_col_mixed["city"], df=df_mixed_dtype)
assert sig_rating == ("placeholder", frozenset({"-1"}), "numeric"), sig_rating
assert sig_hq == ("placeholder", frozenset({"-1"}), "text"), sig_hq
assert sig_hq == sig_city, "headquarters and city are both text-shaped and should still match"
assert sig_rating != sig_hq, "a numeric column must not share a signature with a text column"
print("PASS: dtype category is appended to the signature and separates numeric from text.\n")

mixed_groups = dc._group_issues_by_signature(list(mixed_issues), df=df_mixed_dtype)
sizes_mixed = sorted(len(g) for g in mixed_groups)
assert sizes_mixed == [1, 2], (
    f"expected 'rating' alone (numeric, unique signature) and "
    f"'headquarters'+'city' grouped (text, shared signature), got sizes {sizes_mixed}"
)
text_group = next(g for g in mixed_groups if len(g) == 2)
assert set(text_group) == {by_col_mixed["headquarters"], by_col_mixed["city"]}
print("PASS: dtype-aware grouping keeps the numeric column out of the text columns' batch.\n")

print("=" * 70)
print("ALL ISSUE-BATCHING (SPEC 4) ASSERTIONS PASSED")
print("=" * 70)
