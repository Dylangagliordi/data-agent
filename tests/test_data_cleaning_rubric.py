"""Standalone test for the deterministic rubric checker (check_rubric) — no LLM, no
approval gate, just the rubric logic against real files.

Uses three fixture files under data/_test_etl/mixed/:
- customers_messy.csv: duplicate rows, duplicate customer_id, inconsistent
  capitalization/whitespace in city, inconsistent date formats.
- orders_missing.csv: missing values beyond threshold, a negative amount.
- products_clean.csv: no issues at all — used as the false-positive check.
"""

from utils.data_cleaning import check_rubric

FIXTURE_DIR = "data/_test_etl/mixed"

print("=" * 70)
print("customers_messy.csv (expect: duplicates, categorical, dtype issues)")
print("=" * 70)
issues_customers = check_rubric(f"{FIXTURE_DIR}/customers_messy.csv")
for i in issues_customers:
    print(" -", i)
assert issues_customers, "expected customers_messy.csv to be flagged, got no issues"
assert any("Duplicate rows" in i for i in issues_customers), "expected duplicate row issue"
assert any("Duplicate values" in i and "customer_id" in i for i in issues_customers), (
    "expected duplicate customer_id issue"
)
assert any("Inconsistent categorical values" in i and "city" in i for i in issues_customers), (
    "expected inconsistent categorical values issue for city"
)
print("PASS: customers_messy.csv correctly flagged with expected issue types.")

print("\n" + "=" * 70)
print("orders_missing.csv (expect: missing values, negative amount)")
print("=" * 70)
issues_orders = check_rubric(f"{FIXTURE_DIR}/orders_missing.csv")
for i in issues_orders:
    print(" -", i)
assert issues_orders, "expected orders_missing.csv to be flagged, got no issues"
assert any("Missing values" in i for i in issues_orders), "expected a missing-values issue"
assert any("Invalid values" in i and "amount" in i for i in issues_orders), (
    "expected a negative-amount invalid-value issue"
)
print("PASS: orders_missing.csv correctly flagged with expected issue types.")

print("\n" + "=" * 70)
print("products_clean.csv (expect: NOTHING flagged — false-positive check)")
print("=" * 70)
issues_products = check_rubric(f"{FIXTURE_DIR}/products_clean.csv")
for i in issues_products:
    print(" -", i)
assert not issues_products, f"expected no issues for a clean file, got: {issues_products}"
print("PASS: products_clean.csv correctly produced zero issues (no false positive).")

print("\n" + "=" * 70)
print("ALL RUBRIC CHECKER ASSERTIONS PASSED")
print("=" * 70)
