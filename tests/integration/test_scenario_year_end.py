"""Year-End Close Integration Tests.

Tests the full year-end close workflow: fiscal year closing, closing entries,
retained earnings transfer, opening balance carry-forward, new fiscal year
creation, closed period enforcement, and multi-year continuity.

Cross-skill interaction: erpclaw-gl, erpclaw-journals, erpclaw-reports.
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
)


# ---------------------------------------------------------------------------
# Shared setup helper
# ---------------------------------------------------------------------------

def _setup_year_end_environment(conn):
    """Create a company with accounts needed for year-end close scenarios.

    Returns dict with company_id, fy_id (FY 2025), cost_center_id, and account IDs for:
    - bank (asset, bank)
    - receivable (asset, receivable)
    - revenue (income, revenue)
    - service_revenue (income, revenue)
    - rent_expense (expense, expense)
    - salary_expense (expense, expense)
    - utilities_expense (expense, expense)
    - cogs (expense, cost_of_goods_sold)
    - retained_earnings (equity, equity)
    """
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid, name="FY 2025",
                                    start_date="2025-01-01",
                                    end_date="2025-12-31")
    seed_naming_series(conn, cid, year=2025)
    cc = create_test_cost_center(conn, cid)

    # Asset accounts
    bank = create_test_account(conn, cid, "Bank", "asset",
                               account_type="bank", account_number="1010")
    receivable = create_test_account(conn, cid, "Accounts Receivable", "asset",
                                     account_type="receivable", account_number="1200")

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
    cogs = create_test_account(conn, cid, "Cost of Goods Sold", "expense",
                               account_type="cost_of_goods_sold", account_number="5300")

    return {
        "company_id": cid,
        "fy_id": fy_id,
        "cost_center_id": cc,
        "bank": bank,
        "receivable": receivable,
        "revenue": revenue,
        "service_revenue": service_revenue,
        "rent_expense": rent_expense,
        "salary_expense": salary_expense,
        "utilities_expense": utilities_expense,
        "cogs": cogs,
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


def _post_standard_year_activity(conn, env):
    """Post a year of revenue and expense JEs for testing year-end close.

    Posts:
    - Revenue: 120,000 total (10,000/month for 12 months via Sales Revenue)
    - Expenses: 72,000 total (6,000/month: rent 3000, salary 2500, utilities 500)
    - Net income = 48,000
    """
    cc = env["cost_center_id"]

    for month in range(1, 13):
        month_str = f"{month:02d}"
        posting_date = f"2025-{month_str}-15"

        # Monthly revenue: DR Bank 10000, CR Revenue 10000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "10000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "10000.00",
             "cost_center_id": cc},
        ], posting_date, remark=f"Revenue month {month}")

        # Monthly rent: DR Rent Expense 3000, CR Bank 3000
        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "3000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "3000.00"},
        ], posting_date, remark=f"Rent month {month}")

        # Monthly salary: DR Salary Expense 2500, CR Bank 2500
        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "2500.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "2500.00"},
        ], posting_date, remark=f"Salary month {month}")

        # Monthly utilities: DR Utilities Expense 500, CR Bank 500
        _post_je(conn, env, [
            {"account_id": env["utilities_expense"], "debit": "500.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "500.00"},
        ], posting_date, remark=f"Utilities month {month}")


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestYearEndScenario:
    """Year-end close integration tests."""

    # -------------------------------------------------------------------
    # 1. Full year-end close: revenue/expense JEs -> close FY -> verify
    # -------------------------------------------------------------------

    def test_full_year_end_close(self, fresh_db):
        """Full year-end workflow: post activity, close FY, verify balances."""
        conn = fresh_db
        env = _setup_year_end_environment(conn)

        # Post full year of activity
        _post_standard_year_activity(conn, env)

        # Verify pre-close P&L
        pnl = _call_action("erpclaw-reports", "profit-and-loss", conn,
                           company_id=env["company_id"],
                           from_date="2025-01-01",
                           to_date="2025-12-31")
        assert pnl["status"] == "ok"
        assert Decimal(pnl["income_total"]) == Decimal("120000.00")
        assert Decimal(pnl["expense_total"]) == Decimal("72000.00")
        assert Decimal(pnl["net_income"]) == Decimal("48000.00")

        # Close fiscal year
        result = _call_action("erpclaw-gl", "close-fiscal-year", conn,
                              fiscal_year_id=env["fy_id"],
                              closing_account_id=env["retained_earnings"],
                              company_id=env["company_id"],
                              posting_date="2025-12-31")
        assert result["status"] == "ok"
        assert result["fiscal_year_closed"] is True
        assert Decimal(result["net_pl_transferred"]) == Decimal("48000.00")

        # Verify GL integrity after close
        integrity = _call_action("erpclaw-gl", "check-gl-integrity", conn,
                                 company_id=env["company_id"])
        assert integrity["status"] == "ok"
        assert integrity["balanced"] is True

        # Verify trial balance is balanced after close
        tb = _call_action("erpclaw-reports", "trial-balance", conn,
                          company_id=env["company_id"],
                          to_date="2025-12-31")
        assert tb["status"] == "ok"
        assert Decimal(tb["total_debit"]) == Decimal(tb["total_credit"])

    # -------------------------------------------------------------------
    # 2. Close fiscal year: verify closing entries created
    # -------------------------------------------------------------------

    def test_close_fiscal_year(self, fresh_db):
        """Close FY and verify period closing voucher and closing entries created."""
        conn = fresh_db
        env = _setup_year_end_environment(conn)
        cc = env["cost_center_id"]

        # Simple activity: Revenue 50000, Expenses 30000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "50000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "50000.00",
             "cost_center_id": cc},
        ], "2025-06-15")

        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "30000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "30000.00"},
        ], "2025-07-15")

        # Close FY
        result = _call_action("erpclaw-gl", "close-fiscal-year", conn,
                              fiscal_year_id=env["fy_id"],
                              closing_account_id=env["retained_earnings"],
                              company_id=env["company_id"],
                              posting_date="2025-12-31")
        assert result["status"] == "ok"
        assert result["fiscal_year_closed"] is True
        assert result["gl_entries_created"] > 0

        # Verify period closing voucher exists
        pcv = conn.execute(
            "SELECT * FROM period_closing_voucher WHERE fiscal_year_id = ?",
            (env["fy_id"],),
        ).fetchone()
        assert pcv is not None
        assert pcv["status"] == "submitted"
        assert Decimal(pcv["net_pl_amount"]) == Decimal("20000.00")

        # Verify fiscal year marked as closed
        fy = conn.execute(
            "SELECT is_closed FROM fiscal_year WHERE id = ?",
            (env["fy_id"],),
        ).fetchone()
        assert fy["is_closed"] == 1

    # -------------------------------------------------------------------
    # 3. Closing entries GL: income/expense zeroed, retained earnings updated
    # -------------------------------------------------------------------

    def test_closing_entries_gl(self, fresh_db):
        """Verify closing entries zero out income/expense and credit retained earnings."""
        conn = fresh_db
        env = _setup_year_end_environment(conn)
        cc = env["cost_center_id"]

        # Revenue: 80000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "80000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "80000.00",
             "cost_center_id": cc},
        ], "2025-03-15")

        # Expenses: Rent 25000, Salary 20000 = 45000
        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "25000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "25000.00"},
        ], "2025-06-15")

        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "20000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "20000.00"},
        ], "2025-09-15")

        # Net P&L = 80000 - 45000 = 35000

        # Close FY
        result = _call_action("erpclaw-gl", "close-fiscal-year", conn,
                              fiscal_year_id=env["fy_id"],
                              closing_account_id=env["retained_earnings"],
                              company_id=env["company_id"],
                              posting_date="2025-12-31")
        assert result["status"] == "ok"

        # After closing, income accounts should have net zero when including
        # closing entries (period_closing voucher type)
        for acct_id in (env["revenue"],):
            net = conn.execute(
                """SELECT COALESCE(SUM(CAST(credit AS REAL)), 0) -
                          COALESCE(SUM(CAST(debit AS REAL)), 0) as net
                   FROM gl_entry WHERE account_id = ? AND is_cancelled = 0""",
                (acct_id,),
            ).fetchone()["net"]
            assert abs(net) < 0.01, f"Income account not zeroed: net={net}"

        # Expense accounts should also be zeroed
        for acct_id in (env["rent_expense"], env["salary_expense"]):
            net = conn.execute(
                """SELECT COALESCE(SUM(CAST(debit AS REAL)), 0) -
                          COALESCE(SUM(CAST(credit AS REAL)), 0) as net
                   FROM gl_entry WHERE account_id = ? AND is_cancelled = 0""",
                (acct_id,),
            ).fetchone()["net"]
            assert abs(net) < 0.01, f"Expense account not zeroed: net={net}"

        # Retained earnings should have received the net P&L (35000 credit)
        re_closing = conn.execute(
            """SELECT COALESCE(SUM(CAST(credit AS REAL)), 0) -
                      COALESCE(SUM(CAST(debit AS REAL)), 0) as net
               FROM gl_entry WHERE account_id = ?
               AND voucher_type = 'period_closing' AND is_cancelled = 0""",
            (env["retained_earnings"],),
        ).fetchone()["net"]
        assert abs(re_closing - 35000.0) < 0.01

    # -------------------------------------------------------------------
    # 4. New fiscal year: create after closing old one
    # -------------------------------------------------------------------

    def test_new_fiscal_year(self, fresh_db):
        """Create a new FY after closing the old one via add-fiscal-year action."""
        conn = fresh_db
        env = _setup_year_end_environment(conn)
        cc = env["cost_center_id"]

        # Minimal activity
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "10000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "10000.00",
             "cost_center_id": cc},
        ], "2025-06-15")

        # Close FY 2025
        _call_action("erpclaw-gl", "close-fiscal-year", conn,
                     fiscal_year_id=env["fy_id"],
                     closing_account_id=env["retained_earnings"],
                     company_id=env["company_id"],
                     posting_date="2025-12-31")

        # Create FY 2026
        result = _call_action("erpclaw-gl", "add-fiscal-year", conn,
                              company_id=env["company_id"],
                              name="FY 2026",
                              start_date="2026-01-01",
                              end_date="2026-12-31")
        assert result["status"] == "ok"
        new_fy_id = result["fiscal_year_id"]

        # Verify new FY exists and is not closed
        fy = conn.execute(
            "SELECT * FROM fiscal_year WHERE id = ?",
            (new_fy_id,),
        ).fetchone()
        assert fy is not None
        assert fy["name"] == "FY 2026"
        assert fy["start_date"] == "2026-01-01"
        assert fy["end_date"] == "2026-12-31"
        assert fy["is_closed"] == 0

        # Seed naming series for new year and post to the new FY
        seed_naming_series(conn, env["company_id"], year=2026)
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "5000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "5000.00",
             "cost_center_id": cc},
        ], "2026-01-15", remark="First entry in new FY")

        # Verify the entry is in the new year
        gl = conn.execute(
            """SELECT COUNT(*) as cnt FROM gl_entry
               WHERE posting_date >= '2026-01-01' AND is_cancelled = 0""",
        ).fetchone()["cnt"]
        assert gl >= 2  # at least the 2 GL lines from the JE

    # -------------------------------------------------------------------
    # 5. Opening balances carry forward to new FY
    # -------------------------------------------------------------------

    def test_opening_balances(self, fresh_db):
        """Verify balance sheet accounts carry forward to the new FY."""
        conn = fresh_db
        env = _setup_year_end_environment(conn)
        cc = env["cost_center_id"]

        # Revenue 100000, Expenses 60000 -> Net income 40000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "100000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "100000.00",
             "cost_center_id": cc},
        ], "2025-06-15")

        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "60000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "60000.00"},
        ], "2025-09-15")

        # Bank balance before close = 100000 - 60000 = 40000

        # Close FY 2025
        _call_action("erpclaw-gl", "close-fiscal-year", conn,
                     fiscal_year_id=env["fy_id"],
                     closing_account_id=env["retained_earnings"],
                     company_id=env["company_id"],
                     posting_date="2025-12-31")

        # Create FY 2026
        fy2026_id = create_test_fiscal_year(conn, env["company_id"],
                                            name="FY 2026",
                                            start_date="2026-01-01",
                                            end_date="2026-12-31")

        # Bank balance should carry forward
        # The bank account had: DR 100000, CR 60000 => balance = 40000 debit
        bal = _call_action("erpclaw-gl", "get-account-balance", conn,
                           account_id=env["bank"],
                           as_of_date="2026-01-01",
                           company_id=env["company_id"])
        assert bal["status"] == "ok"
        assert Decimal(bal["balance"]) == Decimal("40000.00")

        # Retained earnings should have the net income = 40000 from closing entries
        bal_re = _call_action("erpclaw-gl", "get-account-balance", conn,
                              account_id=env["retained_earnings"],
                              as_of_date="2026-01-01",
                              company_id=env["company_id"])
        assert bal_re["status"] == "ok"
        assert Decimal(bal_re["balance"]) == Decimal("40000.00")

        # Trial balance in new FY should show opening balances
        tb = _call_action("erpclaw-reports", "trial-balance", conn,
                          company_id=env["company_id"],
                          from_date="2026-01-01",
                          to_date="2026-01-31")
        assert tb["status"] == "ok"
        # Opening balances should exist for BS accounts
        bank_row = next((a for a in tb["accounts"]
                         if a["account_name"] == "Bank"), None)
        assert bank_row is not None
        # TB shows gross opening amounts: DR 100000, CR 60000 (net 40000 debit)
        assert Decimal(bank_row["opening_debit"]) == Decimal("100000.00")
        assert Decimal(bank_row["opening_credit"]) == Decimal("60000.00")

    # -------------------------------------------------------------------
    # 6. Closed period enforcement: can't post to closed FY
    # -------------------------------------------------------------------

    def test_closed_period_enforcement(self, fresh_db):
        """Verify that posting to a closed fiscal year is rejected."""
        conn = fresh_db
        env = _setup_year_end_environment(conn)
        cc = env["cost_center_id"]

        # Minimal activity
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "5000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "5000.00",
             "cost_center_id": cc},
        ], "2025-06-15")

        # Close FY 2025
        _call_action("erpclaw-gl", "close-fiscal-year", conn,
                     fiscal_year_id=env["fy_id"],
                     closing_account_id=env["retained_earnings"],
                     company_id=env["company_id"],
                     posting_date="2025-12-31")

        # Attempt to post a JE in the closed FY period
        lines = json.dumps([
            {"account_id": env["bank"], "debit": "1000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "1000.00",
             "cost_center_id": cc},
        ])
        result = _call_action("erpclaw-journals", "add-journal-entry", conn,
                              company_id=env["company_id"],
                              posting_date="2025-11-15",
                              lines=lines)
        # The JE creation itself may succeed (draft), but submission should fail
        if result["status"] == "ok":
            je_id = result["journal_entry_id"]
            submit_result = _call_action("erpclaw-journals", "submit-journal-entry",
                                         conn, journal_entry_id=je_id,
                                         company_id=env["company_id"])
            # Submission should fail because the period is closed
            assert submit_result["status"] == "error", \
                "Should not be able to submit JE in a closed fiscal year"
        else:
            # Draft creation itself was blocked — also acceptable
            assert result["status"] == "error"

    # -------------------------------------------------------------------
    # 7. Retained earnings = prior year net income
    # -------------------------------------------------------------------

    def test_retained_earnings(self, fresh_db):
        """Verify retained earnings equals prior year net income after close."""
        conn = fresh_db
        env = _setup_year_end_environment(conn)
        cc = env["cost_center_id"]

        # Revenue: 75000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "75000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "75000.00",
             "cost_center_id": cc},
        ], "2025-04-15")

        # Expenses: 45000
        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "20000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "20000.00"},
        ], "2025-07-15")

        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "25000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "25000.00"},
        ], "2025-10-15")

        # Net income = 75000 - 45000 = 30000

        # Close FY
        result = _call_action("erpclaw-gl", "close-fiscal-year", conn,
                              fiscal_year_id=env["fy_id"],
                              closing_account_id=env["retained_earnings"],
                              company_id=env["company_id"],
                              posting_date="2025-12-31")
        assert result["status"] == "ok"
        assert Decimal(result["net_pl_transferred"]) == Decimal("30000.00")

        # Retained earnings should equal the net income from closing entries
        re_bal = conn.execute(
            """SELECT COALESCE(SUM(CAST(credit AS REAL)), 0) -
                      COALESCE(SUM(CAST(debit AS REAL)), 0) as net
               FROM gl_entry WHERE account_id = ?
               AND voucher_type = 'period_closing' AND is_cancelled = 0""",
            (env["retained_earnings"],),
        ).fetchone()["net"]
        assert abs(re_bal - 30000.0) < 0.01, \
            f"Retained earnings {re_bal} should be 30000"

    # -------------------------------------------------------------------
    # 8. Multi-year: close FY 2025, open FY 2026, verify continuity
    # -------------------------------------------------------------------

    def test_multi_year(self, fresh_db):
        """Close FY 2025, create FY 2026, post in FY 2026, verify continuity."""
        conn = fresh_db
        env = _setup_year_end_environment(conn)
        cc = env["cost_center_id"]

        # FY 2025 activity: Revenue 60000, Expenses 35000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "60000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "60000.00",
             "cost_center_id": cc},
        ], "2025-05-15")

        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "35000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "35000.00"},
        ], "2025-08-15")

        # Net income 2025 = 25000
        # Bank balance end of 2025 = 60000 - 35000 = 25000

        # Close FY 2025
        result = _call_action("erpclaw-gl", "close-fiscal-year", conn,
                              fiscal_year_id=env["fy_id"],
                              closing_account_id=env["retained_earnings"],
                              company_id=env["company_id"],
                              posting_date="2025-12-31")
        assert result["status"] == "ok"

        # Create FY 2026
        fy2026_id = create_test_fiscal_year(conn, env["company_id"],
                                            name="FY 2026",
                                            start_date="2026-01-01",
                                            end_date="2026-12-31")
        seed_naming_series(conn, env["company_id"], year=2026)

        # Post FY 2026 activity: Revenue 40000, Expenses 22000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "40000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "40000.00",
             "cost_center_id": cc},
        ], "2026-03-15")

        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "22000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "22000.00"},
        ], "2026-06-15")

        # Verify FY 2026 P&L only shows 2026 activity
        pnl_2026 = _call_action("erpclaw-reports", "profit-and-loss", conn,
                                company_id=env["company_id"],
                                from_date="2026-01-01",
                                to_date="2026-12-31")
        assert pnl_2026["status"] == "ok"
        assert Decimal(pnl_2026["income_total"]) == Decimal("40000.00")
        assert Decimal(pnl_2026["expense_total"]) == Decimal("22000.00")
        assert Decimal(pnl_2026["net_income"]) == Decimal("18000.00")

        # Verify cumulative bank balance = 25000 (from 2025) + 40000 - 22000 = 43000
        bal = _call_action("erpclaw-gl", "get-account-balance", conn,
                           account_id=env["bank"],
                           as_of_date="2026-12-31",
                           company_id=env["company_id"])
        assert bal["status"] == "ok"
        assert Decimal(bal["balance"]) == Decimal("43000.00")

        # Balance sheet at end of 2026 should balance
        bs = _call_action("erpclaw-reports", "balance-sheet", conn,
                          company_id=env["company_id"],
                          as_of_date="2026-12-31")
        assert bs["status"] == "ok"
        total_assets = Decimal(bs["total_assets"])
        total_liabilities = Decimal(bs["total_liabilities"])
        total_equity = Decimal(bs["total_equity"])
        assert total_assets == total_liabilities + total_equity, \
            f"BS not balanced: A={total_assets}, L={total_liabilities}, E={total_equity}"

    # -------------------------------------------------------------------
    # 9. Trial balance after close: balanced
    # -------------------------------------------------------------------

    def test_trial_balance_after_close(self, fresh_db):
        """Verify trial balance is balanced after year-end closing entries."""
        conn = fresh_db
        env = _setup_year_end_environment(conn)
        cc = env["cost_center_id"]

        # Post activity across multiple accounts
        # Revenue via two different accounts
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "30000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "30000.00",
             "cost_center_id": cc},
        ], "2025-03-15")

        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "15000.00", "credit": "0"},
            {"account_id": env["service_revenue"], "debit": "0", "credit": "15000.00",
             "cost_center_id": cc},
        ], "2025-06-15")

        # Expenses via three different accounts
        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "12000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "12000.00"},
        ], "2025-04-15")

        _post_je(conn, env, [
            {"account_id": env["salary_expense"], "debit": "8000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "8000.00"},
        ], "2025-07-15")

        _post_je(conn, env, [
            {"account_id": env["utilities_expense"], "debit": "3000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "3000.00"},
        ], "2025-10-15")

        # Pre-close TB: verify balanced
        tb_pre = _call_action("erpclaw-reports", "trial-balance", conn,
                              company_id=env["company_id"],
                              to_date="2025-12-31")
        assert tb_pre["status"] == "ok"
        assert Decimal(tb_pre["total_debit"]) == Decimal(tb_pre["total_credit"])

        # Close FY
        _call_action("erpclaw-gl", "close-fiscal-year", conn,
                     fiscal_year_id=env["fy_id"],
                     closing_account_id=env["retained_earnings"],
                     company_id=env["company_id"],
                     posting_date="2025-12-31")

        # Post-close TB: verify still balanced
        tb_post = _call_action("erpclaw-reports", "trial-balance", conn,
                               company_id=env["company_id"],
                               to_date="2025-12-31")
        assert tb_post["status"] == "ok"
        assert Decimal(tb_post["total_debit"]) == Decimal(tb_post["total_credit"])

        # The total should have increased because closing entries add to the totals
        assert Decimal(tb_post["total_debit"]) >= Decimal(tb_pre["total_debit"])

    # -------------------------------------------------------------------
    # 10. P&L after close: P&L accounts show zero
    # -------------------------------------------------------------------

    def test_pnl_after_close(self, fresh_db):
        """P&L accounts should show zero after closing entries are posted."""
        conn = fresh_db
        env = _setup_year_end_environment(conn)
        cc = env["cost_center_id"]

        # Revenue: 40000
        _post_je(conn, env, [
            {"account_id": env["bank"], "debit": "40000.00", "credit": "0"},
            {"account_id": env["revenue"], "debit": "0", "credit": "40000.00",
             "cost_center_id": cc},
        ], "2025-05-15")

        # Expenses: 15000
        _post_je(conn, env, [
            {"account_id": env["rent_expense"], "debit": "15000.00", "credit": "0",
             "cost_center_id": cc},
            {"account_id": env["bank"], "debit": "0", "credit": "15000.00"},
        ], "2025-08-15")

        # Pre-close: P&L should show net income 25000
        pnl_pre = _call_action("erpclaw-reports", "profit-and-loss", conn,
                               company_id=env["company_id"],
                               from_date="2025-01-01",
                               to_date="2025-12-31")
        assert pnl_pre["status"] == "ok"
        assert Decimal(pnl_pre["net_income"]) == Decimal("25000.00")

        # Close FY
        _call_action("erpclaw-gl", "close-fiscal-year", conn,
                     fiscal_year_id=env["fy_id"],
                     closing_account_id=env["retained_earnings"],
                     company_id=env["company_id"],
                     posting_date="2025-12-31")

        # After close: when viewing the same period including closing entries,
        # the P&L report for the period should show the closing entries zeroing
        # the income and expense accounts. Verify directly via GL query.
        for acct_id, acct_name in [
            (env["revenue"], "Sales Revenue"),
            (env["rent_expense"], "Rent Expense"),
        ]:
            # Net balance across all GL entries including closing should be zero
            net = conn.execute(
                """SELECT COALESCE(SUM(CAST(debit AS REAL)), 0) -
                          COALESCE(SUM(CAST(credit AS REAL)), 0) as net
                   FROM gl_entry WHERE account_id = ? AND is_cancelled = 0""",
                (acct_id,),
            ).fetchone()["net"]
            assert abs(net) < 0.01, \
                f"{acct_name} not zeroed after close: net={net}"

        # Retained earnings should hold the net income
        re_bal = _call_action("erpclaw-gl", "get-account-balance", conn,
                              account_id=env["retained_earnings"],
                              as_of_date="2025-12-31",
                              company_id=env["company_id"])
        assert re_bal["status"] == "ok"
        assert Decimal(re_bal["balance"]) == Decimal("25000.00")
