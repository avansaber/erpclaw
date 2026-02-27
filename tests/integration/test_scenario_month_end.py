"""Month-End Close Integration Tests.

Tests the full month-end close workflow: journal entry creation for accruals
and adjustments, depreciation runs, trial balance verification, P&L and
balance sheet validation, and cash flow reporting.

Cross-skill interaction: erpclaw-journals, erpclaw-gl, erpclaw-reports,
erpclaw-assets.
"""
import json
import uuid
from decimal import Decimal

from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    seed_naming_series,
    create_test_account,
    create_test_cost_center,
    setup_phase2_environment,
)


# ---------------------------------------------------------------------------
# Shared setup helper
# ---------------------------------------------------------------------------

def _setup_month_end_environment(conn):
    """Create a company with accounts needed for month-end close scenarios.

    Returns dict with company_id, fy_id, cost_center_id, and account IDs for:
    - bank (asset, bank)
    - revenue (income, revenue)
    - service_revenue (income, revenue)
    - rent_expense (expense, expense)
    - salary_expense (expense, expense)
    - utilities_expense (expense, expense)
    - deferred_revenue (liability)
    - accrued_expense (liability, payable)
    - prepaid_insurance (asset)
    - insurance_expense (expense, expense)
    - depreciation_expense (expense, expense)
    - accumulated_depreciation (asset, accumulated_depreciation)
    - fixed_asset_account (asset, fixed_asset)
    - retained_earnings (equity, equity)
    """
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)
    cc = create_test_cost_center(conn, cid)

    # Asset accounts
    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1010")
    prepaid_insurance = create_test_account(conn, cid, "Prepaid Insurance", "asset",
                                            account_type=None, account_number="1300")
    fixed_asset_acct = create_test_account(conn, cid, "Equipment", "asset",
                                           account_type="fixed_asset", account_number="1500")
    accum_dep = create_test_account(conn, cid, "Accumulated Depreciation", "asset",
                                    account_type="accumulated_depreciation",
                                    account_number="1510")

    # Liability accounts
    deferred_revenue = create_test_account(conn, cid, "Deferred Revenue", "liability",
                                           account_type=None, account_number="2100")
    accrued_expense = create_test_account(conn, cid, "Accrued Expenses", "liability",
                                          account_type=None, account_number="2200")

    # Equity accounts
    retained_earnings = create_test_account(conn, cid, "Retained Earnings", "equity",
                                            account_type="equity", account_number="3200")

    # Income accounts
    revenue = create_test_account(conn, cid, "Sales Revenue", "income",
                                  account_type="revenue", account_number="4000")
    service_revenue = create_test_account(conn, cid, "Service Revenue", "income",
                                          account_type="revenue", account_number="4100")

    # Expense accounts
    rent_expense = create_test_account(conn, cid, "Rent Expense", "expense",
                                       account_type="expense", account_number="5000")
    salary_expense = create_test_account(conn, cid, "Salary Expense", "expense",
                                         account_type="expense", account_number="5100")
    utilities_expense = create_test_account(conn, cid, "Utilities Expense", "expense",
                                            account_type="expense", account_number="5200")
    insurance_expense = create_test_account(conn, cid, "Insurance Expense", "expense",
                                            account_type="expense", account_number="5300")
    depreciation_expense = create_test_account(conn, cid, "Depreciation Expense", "expense",
                                               account_type="expense", account_number="5400")

    return {
        "company_id": cid,
        "fy_id": fy_id,
        "cost_center_id": cc,
        "bank": bank,
        "revenue": revenue,
        "service_revenue": service_revenue,
        "rent_expense": rent_expense,
        "salary_expense": salary_expense,
        "utilities_expense": utilities_expense,
        "deferred_revenue": deferred_revenue,
        "accrued_expense": accrued_expense,
        "prepaid_insurance": prepaid_insurance,
        "insurance_expense": insurance_expense,
        "depreciation_expense": depreciation_expense,
        "accumulated_depreciation": accum_dep,
        "fixed_asset_account": fixed_asset_acct,
        "retained_earnings": retained_earnings,
    }


def _post_je(conn, env, lines_data, posting_date, entry_type="journal", remark=None):
    """Helper to create and submit a journal entry. Returns je_id."""
    lines = json.dumps(lines_data)
    result = _call_action("erpclaw-journals", "add-journal-entry", conn,
                          company_id=env["company_id"],
                          posting_date=posting_date,
                          entry_type=entry_type,
                          lines=lines,
                          remark=remark)
    assert result["status"] == "ok", f"add-journal-entry failed: {result}"
    je_id = result["journal_entry_id"]

    result = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                          journal_entry_id=je_id,
                          company_id=env["company_id"])
    assert result["status"] == "ok", f"submit-journal-entry failed: {result}"
    return je_id


def _setup_asset_for_depreciation(conn, env):
    """Create an asset category and asset with depreciation schedule for testing.

    Creates a $12,000 equipment asset with 5-year straight-line depreciation,
    monthly amount = (12000 - 0) / 60 = $200/month.

    Returns asset_id.
    """
    # Create asset category
    result = _call_action("erpclaw-assets", "add-asset-category", conn,
                          company_id=env["company_id"],
                          name="Office Equipment",
                          depreciation_method="straight_line",
                          useful_life_years="5",
                          asset_account_id=env["fixed_asset_account"],
                          depreciation_account_id=env["depreciation_expense"],
                          accumulated_depreciation_account_id=env["accumulated_depreciation"])
    assert result["status"] == "ok", f"add-asset-category failed: {result}"
    category_id = result["asset_category_id"]

    # Create asset
    result = _call_action("erpclaw-assets", "add-asset", conn,
                          company_id=env["company_id"],
                          name="Office Computer",
                          asset_category_id=category_id,
                          gross_value="12000.00",
                          salvage_value="0",
                          purchase_date="2026-01-01",
                          depreciation_start_date="2026-01-31")
    assert result["status"] == "ok", f"add-asset failed: {result}"
    asset_id = result["asset_id"]

    # Set asset to submitted status (no submit action; update directly)
    conn.execute("UPDATE asset SET status = 'submitted' WHERE id = ?", (asset_id,))
    conn.commit()

    # Generate depreciation schedule
    result = _call_action("erpclaw-assets", "generate-depreciation-schedule", conn,
                          asset_id=asset_id)
    assert result["status"] == "ok", f"generate-depreciation-schedule failed: {result}"

    return asset_id


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestMonthEndScenario:
    """Month-end close integration tests."""

    # -------------------------------------------------------------------
    # 1. Full month-end close: JEs, depreciation, reconcile, verify TB
    # -------------------------------------------------------------------

    def test_full_month_end_close(self, fresh_db):
        """Create revenue/expense JEs, run depreciation, verify TB balanced."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # Record January revenue: DR Bank 15000, CR Revenue 15000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "15000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "15000.00",
             "cost_center_id": cc},
        ], "2026-01-15", remark="January sales revenue")

        # Record January rent: DR Rent Expense 3000, CR Bank 3000
        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "3000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "3000.00"},
        ], "2026-01-20", remark="January rent payment")

        # Record January salaries: DR Salary Expense 8000, CR Bank 8000
        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "8000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "8000.00"},
        ], "2026-01-25", remark="January salaries")

        # Set up and run depreciation
        _setup_asset_for_depreciation(conn, env)
        dep_result = _call_action("erpclaw-assets", "run-depreciation", conn,
                                  company_id=env["company_id"],
                                  posting_date="2026-01-31",
                                  cost_center_id=cc)
        assert dep_result["status"] == "ok"
        assert dep_result["entries_posted"] >= 1

        # Verify trial balance is balanced
        tb = _call_action("erpclaw-reports", "trial-balance", conn,
                          company_id=env["company_id"],
                          from_date="2026-01-01",
                          to_date="2026-01-31")
        assert tb["status"] == "ok"
        assert Decimal(tb["total_debit"]) == Decimal(tb["total_credit"])
        assert Decimal(tb["total_debit"]) > Decimal("0")

    # -------------------------------------------------------------------
    # 2. Revenue accrual JE (deferred revenue -> revenue)
    # -------------------------------------------------------------------

    def test_revenue_accrual(self, fresh_db):
        """Create accrual JE moving deferred revenue to earned revenue."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # Initially receive $12,000 deferred revenue (e.g., annual subscription)
        # DR Bank 12000, CR Deferred Revenue 12000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "12000.00", "credit": "0"},
            {"account_id": env["deferred_revenue"], "debit": "0", "credit": "12000.00"},
        ], "2026-01-01", remark="Annual subscription received")

        # Month-end accrual: recognize 1/12 of revenue
        # DR Deferred Revenue 1000, CR Service Revenue 1000
        je_id = _post_je(conn, env, [
            {"account_id": env["deferred_revenue"], "debit": "1000.00", "credit": "0"},
            {"account_id": env["service_revenue"], "debit": "0", "credit": "1000.00",
             "cost_center_id": cc},
        ], "2026-01-31", remark="Recognize January subscription revenue")

        # Verify deferred revenue balance = 11000 (credit normal)
        bal = _call_action("erpclaw-gl", "get-account-balance", conn,
                           account_id=env["deferred_revenue"],
                           as_of_date="2026-01-31",
                           company_id=env["company_id"])
        assert bal["status"] == "ok"
        assert Decimal(bal["balance"]) == Decimal("11000.00")

        # Verify service revenue balance = 1000 (credit normal)
        bal = _call_action("erpclaw-gl", "get-account-balance", conn,
                           account_id=env["service_revenue"],
                           as_of_date="2026-01-31",
                           company_id=env["company_id"])
        assert bal["status"] == "ok"
        assert Decimal(bal["balance"]) == Decimal("1000.00")

        # Verify GL entries exist for the accrual
        gl_rows = conn.execute(
            "SELECT * FROM gl_entry WHERE voucher_id = ? AND is_cancelled = 0",
            (je_id,),
        ).fetchall()
        assert len(gl_rows) == 2

    # -------------------------------------------------------------------
    # 3. Expense accrual (expense -> payable)
    # -------------------------------------------------------------------

    def test_expense_accrual(self, fresh_db):
        """Create accrual JE for expenses incurred but not yet paid."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # Accrue January utilities not yet billed
        # DR Utilities Expense 750, CR Accrued Expenses 750
        je_id = _post_je(conn, env, [
            {"account_id": env["utilities_expense"], "debit": "750.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["accrued_expense"], "debit": "0", "credit": "750.00"},
        ], "2026-01-31", remark="Accrue January utilities")

        # Verify expense balance = 750 (debit normal)
        bal = _call_action("erpclaw-gl", "get-account-balance", conn,
                           account_id=env["utilities_expense"],
                           as_of_date="2026-01-31",
                           company_id=env["company_id"])
        assert bal["status"] == "ok"
        assert Decimal(bal["balance"]) == Decimal("750.00")

        # Verify accrued expense balance = 750 (credit normal)
        bal = _call_action("erpclaw-gl", "get-account-balance", conn,
                           account_id=env["accrued_expense"],
                           as_of_date="2026-01-31",
                           company_id=env["company_id"])
        assert bal["status"] == "ok"
        assert Decimal(bal["balance"]) == Decimal("750.00")

    # -------------------------------------------------------------------
    # 4. Prepaid amortization (prepaid -> expense)
    # -------------------------------------------------------------------

    def test_prepaid_amortization(self, fresh_db):
        """Amortize prepaid insurance over the period."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # Pay 12 months of insurance upfront: DR Prepaid Insurance 6000, CR Bank 6000
        _post_je(conn, env, [
            {"account_id": env["prepaid_insurance"], "debit": "6000.00", "credit": "0"},
            {"account_id": env["bank"], "debit": "0", "credit": "6000.00"},
        ], "2026-01-01", remark="Annual insurance premium")

        # Month-end: amortize 1/12 = $500
        # DR Insurance Expense 500, CR Prepaid Insurance 500
        _post_je(conn, env, [
            {"account_id": env["insurance_expense"], "debit": "500.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["prepaid_insurance"], "debit": "0", "credit": "500.00"},
        ], "2026-01-31", remark="Amortize January insurance")

        # Verify prepaid insurance balance = 5500 (debit normal)
        bal = _call_action("erpclaw-gl", "get-account-balance", conn,
                           account_id=env["prepaid_insurance"],
                           as_of_date="2026-01-31",
                           company_id=env["company_id"])
        assert bal["status"] == "ok"
        assert Decimal(bal["balance"]) == Decimal("5500.00")

        # Verify insurance expense = 500 (debit normal)
        bal = _call_action("erpclaw-gl", "get-account-balance", conn,
                           account_id=env["insurance_expense"],
                           as_of_date="2026-01-31",
                           company_id=env["company_id"])
        assert bal["status"] == "ok"
        assert Decimal(bal["balance"]) == Decimal("500.00")

    # -------------------------------------------------------------------
    # 5. Depreciation run
    # -------------------------------------------------------------------

    def test_depreciation_run(self, fresh_db):
        """Run depreciation and verify GL entries are created."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # Record purchase of equipment: DR Equipment 12000, CR Bank 12000
        _post_je(conn, env, [
            {"account_id": env["fixed_asset_account"], "debit": "12000.00", "credit": "0"},
            {"account_id": env["bank"], "debit": "0", "credit": "12000.00"},
        ], "2026-01-01", remark="Purchase office computer")

        # Create asset and schedule
        asset_id = _setup_asset_for_depreciation(conn, env)

        # Run batch depreciation for January
        result = _call_action("erpclaw-assets", "run-depreciation", conn,
                              company_id=env["company_id"],
                              posting_date="2026-01-31",
                              cost_center_id=cc)
        assert result["status"] == "ok"
        assert result["entries_posted"] == 1

        # Verify GL entries: DR Depreciation Expense 200, CR Accumulated Dep 200
        # (12000 / 60 months = 200/month)
        gl_rows = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'depreciation_entry' AND is_cancelled = 0""",
        ).fetchall()
        assert len(gl_rows) == 2

        total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
        total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
        assert total_debit == Decimal("200.00")
        assert total_credit == Decimal("200.00")

        # Verify asset book value updated
        asset = conn.execute("SELECT * FROM asset WHERE id = ?", (asset_id,)).fetchone()
        assert Decimal(asset["current_book_value"]) == Decimal("11800.00")
        assert Decimal(asset["accumulated_depreciation"]) == Decimal("200.00")

    # -------------------------------------------------------------------
    # 6. Trial balance is balanced (DR = CR)
    # -------------------------------------------------------------------

    def test_trial_balance_balanced(self, fresh_db):
        """Verify trial balance debits equal credits after various postings."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # Post multiple JEs to build activity
        # Revenue
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "20000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "20000.00",
             "cost_center_id": cc},
        ], "2026-01-10")

        # Rent
        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "5000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "5000.00"},
        ], "2026-01-15")

        # Salaries
        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "10000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "10000.00"},
        ], "2026-01-20")

        # Accrual
        _post_je(conn, env, [
            {"account_id": env["utilities_expense"], "debit": "800.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["accrued_expense"], "debit": "0", "credit": "800.00"},
        ], "2026-01-31")

        # Verify trial balance
        tb = _call_action("erpclaw-reports", "trial-balance", conn,
                          company_id=env["company_id"],
                          to_date="2026-01-31")
        assert tb["status"] == "ok"
        assert Decimal(tb["total_debit"]) == Decimal(tb["total_credit"])
        # Verify the total is nonzero (we actually posted things)
        assert Decimal(tb["total_debit"]) > Decimal("0")

    # -------------------------------------------------------------------
    # 7. Profit & Loss shows correct net income
    # -------------------------------------------------------------------

    def test_profit_and_loss(self, fresh_db):
        """Verify P&L calculates correct net income from revenue and expenses."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # Revenue: 25000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "25000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "25000.00",
             "cost_center_id": cc},
        ], "2026-01-10")

        # Rent expense: 4000
        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "4000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "4000.00"},
        ], "2026-01-15")

        # Salary expense: 12000
        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "12000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "12000.00"},
        ], "2026-01-20")

        # Utilities expense: 1500
        _post_je(conn, env, [
            {"account_id": env["utilities_expense"], "debit": "1500.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "1500.00"},
        ], "2026-01-25")

        # P&L: Net income = 25000 - (4000 + 12000 + 1500) = 7500
        pnl = _call_action("erpclaw-reports", "profit-and-loss", conn,
                           company_id=env["company_id"],
                           from_date="2026-01-01",
                           to_date="2026-01-31")
        assert pnl["status"] == "ok"
        assert Decimal(pnl["income_total"]) == Decimal("25000.00")
        assert Decimal(pnl["expense_total"]) == Decimal("17500.00")
        assert Decimal(pnl["net_income"]) == Decimal("7500.00")

        # Verify individual line items exist
        income_accounts = {item["account"]: item["amount"] for item in pnl["income"]}
        assert "Sales Revenue" in income_accounts
        assert Decimal(income_accounts["Sales Revenue"]) == Decimal("25000.00")

        expense_accounts = {item["account"]: item["amount"] for item in pnl["expenses"]}
        assert "Rent Expense" in expense_accounts
        assert Decimal(expense_accounts["Rent Expense"]) == Decimal("4000.00")
        assert Decimal(expense_accounts["Salary Expense"]) == Decimal("12000.00")

    # -------------------------------------------------------------------
    # 8. Balance sheet: Assets = Liabilities + Equity
    # -------------------------------------------------------------------

    def test_balance_sheet(self, fresh_db):
        """Verify the fundamental accounting equation holds on the balance sheet."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # Revenue: DR Bank 30000, CR Revenue 30000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "30000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "30000.00",
             "cost_center_id": cc},
        ], "2026-01-10")

        # Expenses: DR Rent 5000, CR Bank 5000
        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "5000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "5000.00"},
        ], "2026-01-15")

        # Accrued liability: DR Salary 10000, CR Accrued Expenses 10000
        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "10000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["accrued_expense"], "debit": "0", "credit": "10000.00"},
        ], "2026-01-20")

        # Balance Sheet: Assets = Liabilities + Equity
        # Bank = 30000 - 5000 = 25000 (asset)
        # Accrued Expenses = 10000 (liability)
        # Net income (included in equity) = 30000 - 5000 - 10000 = 15000
        # Assets (25000) = Liabilities (10000) + Equity (15000)
        bs = _call_action("erpclaw-reports", "balance-sheet", conn,
                          company_id=env["company_id"],
                          as_of_date="2026-01-31")
        assert bs["status"] == "ok"

        total_assets = Decimal(bs["total_assets"])
        total_liabilities = Decimal(bs["total_liabilities"])
        total_equity = Decimal(bs["total_equity"])

        # The accounting equation must hold
        assert total_assets == total_liabilities + total_equity, \
            f"BS not balanced: Assets {total_assets} != L {total_liabilities} + E {total_equity}"

        # Verify specific values
        assert total_assets == Decimal("25000.00")
        assert total_liabilities == Decimal("10000.00")

    # -------------------------------------------------------------------
    # 9. Cash flow report categories
    # -------------------------------------------------------------------

    def test_cash_flow_report(self, fresh_db):
        """Verify cash flow report shows correct opening/closing and net change."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # Cash inflow from revenue
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "20000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "20000.00",
             "cost_center_id": cc},
        ], "2026-01-10")

        # Cash outflow for rent
        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "3000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "3000.00"},
        ], "2026-01-15")

        # Cash outflow for salaries
        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "7000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "7000.00"},
        ], "2026-01-20")

        # Cash flow report
        cf = _call_action("erpclaw-reports", "cash-flow", conn,
                          company_id=env["company_id"],
                          from_date="2026-01-01",
                          to_date="2026-01-31")
        assert cf["status"] == "ok"

        # Opening balance should be 0 (no prior activity)
        assert Decimal(cf["opening_balance"]) == Decimal("0")

        # Closing balance = 20000 - 3000 - 7000 = 10000
        assert Decimal(cf["closing_balance"]) == Decimal("10000.00")

        # Net change = closing - opening
        assert Decimal(cf["net_change"]) == Decimal("10000.00")

    # -------------------------------------------------------------------
    # 10. Month-end with multiple adjustment JEs
    # -------------------------------------------------------------------

    def test_month_end_with_adjustments(self, fresh_db):
        """Multiple adjustment JEs at month-end, verify final trial balance."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # Operating JEs throughout January
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "50000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "50000.00",
             "cost_center_id": cc},
        ], "2026-01-05", remark="January sales")

        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "6000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "6000.00"},
        ], "2026-01-10", remark="Office rent")

        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "20000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "20000.00"},
        ], "2026-01-15", remark="Salaries")

        # Month-end adjustments
        # 1. Accrue utilities
        _post_je(conn, env, [
            {"account_id": env["utilities_expense"], "debit": "1200.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["accrued_expense"], "debit": "0", "credit": "1200.00"},
        ], "2026-01-31", remark="Accrue January utilities")

        # 2. Amortize prepaid insurance
        _post_je(conn, env, [
            {"account_id": env["prepaid_insurance"], "debit": "3600.00", "credit": "0"},
            {"account_id": env["bank"], "debit": "0", "credit": "3600.00"},
        ], "2026-01-02", remark="Prepaid insurance")
        _post_je(conn, env, [
            {"account_id": env["insurance_expense"], "debit": "300.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["prepaid_insurance"], "debit": "0", "credit": "300.00"},
        ], "2026-01-31", remark="Amortize January insurance")

        # 3. Revenue accrual
        _post_je(conn, env, [
            {"account_id": env["deferred_revenue"], "debit": "2000.00", "credit": "0"},
            {"account_id": env["service_revenue"], "debit": "0", "credit": "2000.00",
             "cost_center_id": cc},
        ], "2026-01-31", remark="Recognize deferred revenue")

        # Verify final trial balance
        tb = _call_action("erpclaw-reports", "trial-balance", conn,
                          company_id=env["company_id"],
                          from_date="2026-01-01",
                          to_date="2026-01-31")
        assert tb["status"] == "ok"
        assert Decimal(tb["total_debit"]) == Decimal(tb["total_credit"])

        # Verify P&L reflects all adjustments
        # Income: 50000 + 2000 = 52000
        # Expenses: 6000 + 20000 + 1200 + 300 = 27500
        pnl = _call_action("erpclaw-reports", "profit-and-loss", conn,
                           company_id=env["company_id"],
                           from_date="2026-01-01",
                           to_date="2026-01-31")
        assert pnl["status"] == "ok"
        assert Decimal(pnl["income_total"]) == Decimal("52000.00")
        assert Decimal(pnl["expense_total"]) == Decimal("27500.00")
        assert Decimal(pnl["net_income"]) == Decimal("24500.00")

    # -------------------------------------------------------------------
    # 11. Interperiod comparison (JEs in 2 months)
    # -------------------------------------------------------------------

    def test_interperiod_comparison(self, fresh_db):
        """JEs in two different months; verify period-specific reports."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # January: Revenue 10000, Expenses 6000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "10000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "10000.00",
             "cost_center_id": cc},
        ], "2026-01-15")

        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "6000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "6000.00"},
        ], "2026-01-20")

        # February: Revenue 15000, Expenses 9000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "15000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "15000.00",
             "cost_center_id": cc},
        ], "2026-02-10")

        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "9000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "9000.00"},
        ], "2026-02-15")

        # Verify January P&L
        pnl_jan = _call_action("erpclaw-reports", "profit-and-loss", conn,
                               company_id=env["company_id"],
                               from_date="2026-01-01",
                               to_date="2026-01-31")
        assert pnl_jan["status"] == "ok"
        assert Decimal(pnl_jan["income_total"]) == Decimal("10000.00")
        assert Decimal(pnl_jan["expense_total"]) == Decimal("6000.00")
        assert Decimal(pnl_jan["net_income"]) == Decimal("4000.00")

        # Verify February P&L
        pnl_feb = _call_action("erpclaw-reports", "profit-and-loss", conn,
                               company_id=env["company_id"],
                               from_date="2026-02-01",
                               to_date="2026-02-28")
        assert pnl_feb["status"] == "ok"
        assert Decimal(pnl_feb["income_total"]) == Decimal("15000.00")
        assert Decimal(pnl_feb["expense_total"]) == Decimal("9000.00")
        assert Decimal(pnl_feb["net_income"]) == Decimal("6000.00")

        # Verify cumulative (Jan+Feb) trial balance still balanced
        tb = _call_action("erpclaw-reports", "trial-balance", conn,
                          company_id=env["company_id"],
                          from_date="2026-01-01",
                          to_date="2026-02-28")
        assert tb["status"] == "ok"
        assert Decimal(tb["total_debit"]) == Decimal(tb["total_credit"])

        # Verify the February TB has opening balances from January
        tb_feb = _call_action("erpclaw-reports", "trial-balance", conn,
                              company_id=env["company_id"],
                              from_date="2026-02-01",
                              to_date="2026-02-28")
        assert tb_feb["status"] == "ok"
        # There should be opening balances for accounts used in January
        bank_row = next((a for a in tb_feb["accounts"]
                         if a["account_name"] == "Bank"), None)
        assert bank_row is not None
        # Bank opening: gross debit=10000, gross credit=6000 (net 4000 debit)
        assert Decimal(bank_row["opening_debit"]) == Decimal("10000.00")
        assert Decimal(bank_row["opening_credit"]) == Decimal("6000.00")

    # -------------------------------------------------------------------
    # 12. Trial balance date range filter
    # -------------------------------------------------------------------

    def test_tb_filters(self, fresh_db):
        """Verify trial balance correctly filters by date range."""
        conn = fresh_db
        env = _setup_month_end_environment(conn)
        cc = env["cost_center_id"]

        # Post in January
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "5000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "5000.00",
             "cost_center_id": cc},
        ], "2026-01-15")

        # Post in February
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "8000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "8000.00",
             "cost_center_id": cc},
        ], "2026-02-15")

        # Post in March
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "3000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "3000.00",
             "cost_center_id": cc},
        ], "2026-03-15")

        # TB for January only — should show only January activity in period columns
        tb_jan = _call_action("erpclaw-reports", "trial-balance", conn,
                              company_id=env["company_id"],
                              from_date="2026-01-01",
                              to_date="2026-01-31")
        assert tb_jan["status"] == "ok"
        # Total closing debit should be 5000 (only January bank debit)
        bank_jan = next((a for a in tb_jan["accounts"]
                         if a["account_name"] == "Bank"), None)
        assert bank_jan is not None
        assert Decimal(bank_jan["closing_debit"]) == Decimal("5000.00")

        # TB through end of March — should show all 3 months
        tb_all = _call_action("erpclaw-reports", "trial-balance", conn,
                              company_id=env["company_id"],
                              to_date="2026-03-31")
        assert tb_all["status"] == "ok"
        bank_all = next((a for a in tb_all["accounts"]
                         if a["account_name"] == "Bank"), None)
        assert bank_all is not None
        # Bank closing debit = 5000 + 8000 + 3000 = 16000
        assert Decimal(bank_all["closing_debit"]) == Decimal("16000.00")

        # TB for February only — should show Jan as opening, Feb as period
        tb_feb = _call_action("erpclaw-reports", "trial-balance", conn,
                              company_id=env["company_id"],
                              from_date="2026-02-01",
                              to_date="2026-02-28")
        assert tb_feb["status"] == "ok"
        bank_feb = next((a for a in tb_feb["accounts"]
                         if a["account_name"] == "Bank"), None)
        assert bank_feb is not None
        # Opening from Jan = 5000 debit, period Feb = 8000 debit
        assert Decimal(bank_feb["opening_debit"]) == Decimal("5000.00")
        assert Decimal(bank_feb["debit"]) == Decimal("8000.00")
        assert Decimal(bank_feb["closing_debit"]) == Decimal("13000.00")
