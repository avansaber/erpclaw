"""Multi-Company Isolation Scenario Tests.

Tests that two independent companies operate in full isolation:
- Separate chart of accounts, fiscal years, cost centers
- Journal entries scoped to their respective companies
- Trial balance, balance sheet, and P&L reflect only their own data
- Cross-company account usage is rejected by GL validation (Step 3)
- Customers and suppliers belong to specific companies
"""
import json
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
# Shared setup: creates two fully provisioned company environments
# ---------------------------------------------------------------------------

def _setup_two_companies(conn):
    """Create two complete company environments with FY, naming series,
    cost centers, and company-specific accounts.

    Each company gets its own set of accounts with company-specific account
    numbers (e.g., 1001-AC, 1001-BI) to avoid the global UNIQUE constraint
    on account.account_number.

    Returns a dict with keys 'a' and 'b', each containing:
        company_id, fy_id, cost_center_id, cash_id, revenue_id, expense_id,
        receivable_id, payable_id, retained_id
    """
    envs = {}
    for label, company_name, abbr in [
        ("a", "Alpha Corp", "AC"),
        ("b", "Beta Inc", "BI"),
    ]:
        # 1. Create company via setup skill
        result = _call_action(
            "erpclaw-setup", "setup-company", conn,
            name=company_name, abbr=abbr,
            currency="USD", country="United States",
            fiscal_year_start_month="1",
        )
        assert result["status"] == "ok", f"Failed to create {company_name}: {result}"
        cid = result["company_id"]

        # 2. Create fiscal year via GL skill
        result = _call_action(
            "erpclaw-gl", "add-fiscal-year", conn,
            company_id=cid, name=f"FY 2026 {abbr}",
            start_date="2026-01-01", end_date="2026-12-31",
        )
        assert result["status"] == "ok", f"Failed to create FY for {company_name}"
        fy_id = result["fiscal_year_id"]

        # 3. Seed naming series via GL skill
        result = _call_action(
            "erpclaw-gl", "seed-naming-series", conn,
            company_id=cid,
        )
        assert result["status"] == "ok"

        # 4. Create cost center
        cc_id = create_test_cost_center(conn, cid, name=f"Main - {abbr}")

        # 5. Create company-specific accounts
        #    Account numbers are suffixed with company abbreviation to satisfy
        #    the global UNIQUE constraint on account.account_number.
        cash_id = create_test_account(
            conn, cid, f"Cash - {abbr}", "asset",
            account_type="bank", account_number=f"1001-{abbr}",
        )
        revenue_id = create_test_account(
            conn, cid, f"Revenue - {abbr}", "income",
            account_type="revenue", account_number=f"4001-{abbr}",
        )
        expense_id = create_test_account(
            conn, cid, f"Operating Expense - {abbr}", "expense",
            account_type="expense", account_number=f"5001-{abbr}",
        )
        receivable_id = create_test_account(
            conn, cid, f"Accounts Receivable - {abbr}", "asset",
            account_type="receivable", account_number=f"1200-{abbr}",
        )
        payable_id = create_test_account(
            conn, cid, f"Accounts Payable - {abbr}", "liability",
            account_type="payable", account_number=f"2000-{abbr}",
        )
        retained_id = create_test_account(
            conn, cid, f"Retained Earnings - {abbr}", "equity",
            account_type="equity", account_number=f"3200-{abbr}",
        )

        envs[label] = {
            "company_id": cid,
            "company_name": company_name,
            "abbr": abbr,
            "fy_id": fy_id,
            "cost_center_id": cc_id,
            "cash_id": cash_id,
            "revenue_id": revenue_id,
            "expense_id": expense_id,
            "receivable_id": receivable_id,
            "payable_id": payable_id,
            "retained_id": retained_id,
        }

    return envs


# ---------------------------------------------------------------------------
# Helper: post a journal entry and submit it
# ---------------------------------------------------------------------------

def _post_journal_entry(conn, company_id, lines_data, posting_date="2026-06-15"):
    """Create and submit a journal entry. Returns (je_id, gl_count)."""
    lines = json.dumps(lines_data)
    result = _call_action(
        "erpclaw-journals", "add-journal-entry", conn,
        company_id=company_id, posting_date=posting_date, lines=lines,
    )
    assert result["status"] == "ok", f"add-journal-entry failed: {result}"
    je_id = result["journal_entry_id"]

    result = _call_action(
        "erpclaw-journals", "submit-journal-entry", conn,
        journal_entry_id=je_id, company_id=company_id,
    )
    assert result["status"] == "ok", f"submit-journal-entry failed: {result}"
    gl_count = result["gl_entries_created"]
    return je_id, gl_count


# ===========================================================================
# Test Class
# ===========================================================================

class TestMultiCompanyScenario:
    """Multi-company data isolation scenario tests."""

    # -------------------------------------------------------------------
    # 1. Full end-to-end multi-company isolation
    # -------------------------------------------------------------------

    def test_full_multi_company_isolation(self, fresh_db):
        """Two companies with journal entries each have completely separate
        trial balances. This is the comprehensive end-to-end test."""
        conn = fresh_db
        envs = _setup_two_companies(conn)
        a = envs["a"]
        b = envs["b"]

        # --- Company A: DR Cash 10,000 / CR Revenue 10,000 ---
        _post_journal_entry(conn, a["company_id"], [
            {"account_id": a["cash_id"], "debit": "10000.00", "credit": "0"},
            {"account_id": a["revenue_id"], "debit": "0", "credit": "10000.00",
             "cost_center_id": a["cost_center_id"]},
        ], posting_date="2026-03-01")

        # --- Company B: DR Cash 25,000 / CR Revenue 25,000 ---
        _post_journal_entry(conn, b["company_id"], [
            {"account_id": b["cash_id"], "debit": "25000.00", "credit": "0"},
            {"account_id": b["revenue_id"], "debit": "0", "credit": "25000.00",
             "cost_center_id": b["cost_center_id"]},
        ], posting_date="2026-03-01")

        # --- Company A: DR Expense 3,000 / CR Cash 3,000 ---
        _post_journal_entry(conn, a["company_id"], [
            {"account_id": a["expense_id"], "debit": "3000.00", "credit": "0",
             "cost_center_id": a["cost_center_id"]},
            {"account_id": a["cash_id"], "debit": "0", "credit": "3000.00"},
        ], posting_date="2026-04-01")

        # --- Company B: DR Expense 8,000 / CR Cash 8,000 ---
        _post_journal_entry(conn, b["company_id"], [
            {"account_id": b["expense_id"], "debit": "8000.00", "credit": "0",
             "cost_center_id": b["cost_center_id"]},
            {"account_id": b["cash_id"], "debit": "0", "credit": "8000.00"},
        ], posting_date="2026-04-01")

        # --- Verify Company A trial balance ---
        tb_a = _call_action(
            "erpclaw-reports", "trial-balance", conn,
            company_id=a["company_id"], to_date="2026-12-31",
        )
        assert tb_a["status"] == "ok"
        # Company A total debits = 10000 + 3000 = 13000
        assert Decimal(tb_a["total_debit"]) == Decimal("13000.00")
        assert Decimal(tb_a["total_credit"]) == Decimal("13000.00")

        # Verify Company A has no trace of Company B's 25,000 or 8,000
        a_accounts = {acct["account_id"]: acct for acct in tb_a["accounts"]}
        assert b["cash_id"] not in a_accounts
        assert b["revenue_id"] not in a_accounts
        assert b["expense_id"] not in a_accounts

        # --- Verify Company B trial balance ---
        tb_b = _call_action(
            "erpclaw-reports", "trial-balance", conn,
            company_id=b["company_id"], to_date="2026-12-31",
        )
        assert tb_b["status"] == "ok"
        # Company B total debits = 25000 + 8000 = 33000
        assert Decimal(tb_b["total_debit"]) == Decimal("33000.00")
        assert Decimal(tb_b["total_credit"]) == Decimal("33000.00")

        # Verify Company B has no trace of Company A's 10,000 or 3,000
        b_accounts = {acct["account_id"]: acct for acct in tb_b["accounts"]}
        assert a["cash_id"] not in b_accounts
        assert a["revenue_id"] not in b_accounts
        assert a["expense_id"] not in b_accounts

        # --- Cross-verify: GL entries are properly scoped ---
        for label, env in envs.items():
            gl_rows = conn.execute(
                """SELECT ge.* FROM gl_entry ge
                   JOIN account acc ON ge.account_id = acc.id
                   WHERE acc.company_id = ? AND ge.is_cancelled = 0""",
                (env["company_id"],),
            ).fetchall()
            # Each company has 2 JEs x 2 GL entries each = 4 GL entries
            assert len(gl_rows) == 4, (
                f"Company {label} should have 4 GL entries, got {len(gl_rows)}"
            )

    # -------------------------------------------------------------------
    # 2. Two companies created independently with CoA
    # -------------------------------------------------------------------

    def test_two_companies_created(self, fresh_db):
        """Create two independent companies and verify each has its own
        accounts, fiscal year, cost center, and naming series."""
        conn = fresh_db
        envs = _setup_two_companies(conn)

        # Verify both companies exist in DB
        companies = conn.execute("SELECT * FROM company").fetchall()
        assert len(companies) == 2

        company_ids = {r["id"] for r in companies}
        assert envs["a"]["company_id"] in company_ids
        assert envs["b"]["company_id"] in company_ids

        # Verify each company has exactly 6 accounts (cash, revenue, expense,
        # receivable, payable, retained earnings)
        for label in ("a", "b"):
            cid = envs[label]["company_id"]
            acct_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM account WHERE company_id = ?",
                (cid,),
            ).fetchone()["cnt"]
            assert acct_count == 6, (
                f"Company {label} should have 6 accounts, got {acct_count}"
            )

        # Verify accounts are distinct between companies (no shared IDs)
        a_acct_ids = {r["id"] for r in conn.execute(
            "SELECT id FROM account WHERE company_id = ?",
            (envs["a"]["company_id"],),
        ).fetchall()}
        b_acct_ids = {r["id"] for r in conn.execute(
            "SELECT id FROM account WHERE company_id = ?",
            (envs["b"]["company_id"],),
        ).fetchall()}
        assert a_acct_ids.isdisjoint(b_acct_ids), \
            "Account IDs must not overlap between companies"

        # Verify account numbers are also distinct (due to company suffix)
        a_nums = {r["account_number"] for r in conn.execute(
            "SELECT account_number FROM account WHERE company_id = ?",
            (envs["a"]["company_id"],),
        ).fetchall()}
        b_nums = {r["account_number"] for r in conn.execute(
            "SELECT account_number FROM account WHERE company_id = ?",
            (envs["b"]["company_id"],),
        ).fetchall()}
        assert a_nums.isdisjoint(b_nums), \
            "Account numbers must not overlap between companies"

        # Verify each company has its own fiscal year
        for label in ("a", "b"):
            cid = envs[label]["company_id"]
            fy_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM fiscal_year WHERE company_id = ?",
                (cid,),
            ).fetchone()["cnt"]
            assert fy_count == 1

        # Verify separate cost centers
        for label in ("a", "b"):
            cid = envs[label]["company_id"]
            cc_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM cost_center WHERE company_id = ?",
                (cid,),
            ).fetchone()["cnt"]
            assert cc_count >= 1

        # Verify each company has naming series entries
        for label in ("a", "b"):
            cid = envs[label]["company_id"]
            ns_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM naming_series WHERE company_id = ?",
                (cid,),
            ).fetchone()["cnt"]
            assert ns_count > 0, (
                f"Company {label} should have naming series, got {ns_count}"
            )

    # -------------------------------------------------------------------
    # 3. Company A journal entry
    # -------------------------------------------------------------------

    def test_company_a_journal_entry(self, fresh_db):
        """Create and submit a journal entry in Company A only.
        Verify GL entries exist for Company A and nothing for Company B."""
        conn = fresh_db
        envs = _setup_two_companies(conn)
        a = envs["a"]
        b = envs["b"]

        je_id, gl_count = _post_journal_entry(conn, a["company_id"], [
            {"account_id": a["cash_id"], "debit": "5000.00", "credit": "0"},
            {"account_id": a["revenue_id"], "debit": "0", "credit": "5000.00",
             "cost_center_id": a["cost_center_id"]},
        ])
        assert gl_count == 2

        # GL entries should reference Company A's accounts
        gl_rows = conn.execute(
            "SELECT * FROM gl_entry WHERE voucher_type='journal_entry' "
            "AND voucher_id=? AND is_cancelled=0",
            (je_id,),
        ).fetchall()
        assert len(gl_rows) == 2

        total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
        total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
        assert total_debit == Decimal("5000.00")
        assert total_credit == Decimal("5000.00")

        # All GL entries should use Company A accounts
        gl_account_ids = {r["account_id"] for r in gl_rows}
        assert gl_account_ids == {a["cash_id"], a["revenue_id"]}

        # Company B should have zero GL entries
        b_gl_count = conn.execute(
            """SELECT COUNT(*) as cnt FROM gl_entry ge
               JOIN account acc ON ge.account_id = acc.id
               WHERE acc.company_id = ? AND ge.is_cancelled = 0""",
            (b["company_id"],),
        ).fetchone()["cnt"]
        assert b_gl_count == 0, \
            f"Company B should have 0 GL entries, got {b_gl_count}"

    # -------------------------------------------------------------------
    # 4. Company B journal entry
    # -------------------------------------------------------------------

    def test_company_b_journal_entry(self, fresh_db):
        """Create and submit a journal entry in Company B only.
        Verify GL entries exist for Company B and nothing for Company A."""
        conn = fresh_db
        envs = _setup_two_companies(conn)
        a = envs["a"]
        b = envs["b"]

        je_id, gl_count = _post_journal_entry(conn, b["company_id"], [
            {"account_id": b["cash_id"], "debit": "15000.00", "credit": "0"},
            {"account_id": b["revenue_id"], "debit": "0", "credit": "15000.00",
             "cost_center_id": b["cost_center_id"]},
        ])
        assert gl_count == 2

        # GL entries should reference Company B's accounts
        gl_rows = conn.execute(
            "SELECT * FROM gl_entry WHERE voucher_type='journal_entry' "
            "AND voucher_id=? AND is_cancelled=0",
            (je_id,),
        ).fetchall()
        assert len(gl_rows) == 2

        total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
        total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
        assert total_debit == Decimal("15000.00")
        assert total_credit == Decimal("15000.00")

        gl_account_ids = {r["account_id"] for r in gl_rows}
        assert gl_account_ids == {b["cash_id"], b["revenue_id"]}

        # Company A should have zero GL entries
        a_gl_count = conn.execute(
            """SELECT COUNT(*) as cnt FROM gl_entry ge
               JOIN account acc ON ge.account_id = acc.id
               WHERE acc.company_id = ? AND ge.is_cancelled = 0""",
            (a["company_id"],),
        ).fetchone()["cnt"]
        assert a_gl_count == 0, \
            f"Company A should have 0 GL entries, got {a_gl_count}"

    # -------------------------------------------------------------------
    # 5. Company A trial balance
    # -------------------------------------------------------------------

    def test_company_a_trial_balance(self, fresh_db):
        """Trial balance for Company A shows only Company A transactions.
        Company B transactions must not leak into Company A's report."""
        conn = fresh_db
        envs = _setup_two_companies(conn)
        a = envs["a"]
        b = envs["b"]

        # Post transactions in BOTH companies
        _post_journal_entry(conn, a["company_id"], [
            {"account_id": a["cash_id"], "debit": "7500.00", "credit": "0"},
            {"account_id": a["revenue_id"], "debit": "0", "credit": "7500.00",
             "cost_center_id": a["cost_center_id"]},
        ], posting_date="2026-02-15")

        _post_journal_entry(conn, b["company_id"], [
            {"account_id": b["cash_id"], "debit": "50000.00", "credit": "0"},
            {"account_id": b["revenue_id"], "debit": "0", "credit": "50000.00",
             "cost_center_id": b["cost_center_id"]},
        ], posting_date="2026-02-15")

        # Get Company A trial balance
        tb = _call_action(
            "erpclaw-reports", "trial-balance", conn,
            company_id=a["company_id"], to_date="2026-12-31",
        )
        assert tb["status"] == "ok"

        # Company A totals: DR 7500, CR 7500
        assert Decimal(tb["total_debit"]) == Decimal("7500.00")
        assert Decimal(tb["total_credit"]) == Decimal("7500.00")

        # Find specific account entries
        accts = {acct["account_id"]: acct for acct in tb["accounts"]}
        assert a["cash_id"] in accts
        assert Decimal(accts[a["cash_id"]]["closing_debit"]) == Decimal("7500.00")

        assert a["revenue_id"] in accts
        assert Decimal(accts[a["revenue_id"]]["closing_credit"]) == Decimal("7500.00")

        # Company B accounts must NOT appear in Company A's TB
        assert b["cash_id"] not in accts
        assert b["revenue_id"] not in accts

    # -------------------------------------------------------------------
    # 6. Company B trial balance
    # -------------------------------------------------------------------

    def test_company_b_trial_balance(self, fresh_db):
        """Trial balance for Company B shows only Company B transactions.
        Company A transactions must not leak into Company B's report."""
        conn = fresh_db
        envs = _setup_two_companies(conn)
        a = envs["a"]
        b = envs["b"]

        # Post transactions in BOTH companies
        _post_journal_entry(conn, a["company_id"], [
            {"account_id": a["cash_id"], "debit": "12000.00", "credit": "0"},
            {"account_id": a["revenue_id"], "debit": "0", "credit": "12000.00",
             "cost_center_id": a["cost_center_id"]},
        ], posting_date="2026-05-10")

        _post_journal_entry(conn, b["company_id"], [
            {"account_id": b["cash_id"], "debit": "30000.00", "credit": "0"},
            {"account_id": b["revenue_id"], "debit": "0", "credit": "30000.00",
             "cost_center_id": b["cost_center_id"]},
        ], posting_date="2026-05-10")

        _post_journal_entry(conn, b["company_id"], [
            {"account_id": b["expense_id"], "debit": "5000.00", "credit": "0",
             "cost_center_id": b["cost_center_id"]},
            {"account_id": b["cash_id"], "debit": "0", "credit": "5000.00"},
        ], posting_date="2026-06-01")

        # Get Company B trial balance
        tb = _call_action(
            "erpclaw-reports", "trial-balance", conn,
            company_id=b["company_id"], to_date="2026-12-31",
        )
        assert tb["status"] == "ok"

        # Company B totals: DR (30000 + 5000) = 35000, CR (30000 + 5000) = 35000
        assert Decimal(tb["total_debit"]) == Decimal("35000.00")
        assert Decimal(tb["total_credit"]) == Decimal("35000.00")

        # Verify specific Company B accounts
        # Note: closing_debit/closing_credit are gross sums, not net amounts.
        accts = {acct["account_id"]: acct for acct in tb["accounts"]}

        # Cash: DR 30000 from JE1, CR 5000 from JE2
        assert b["cash_id"] in accts
        cash_entry = accts[b["cash_id"]]
        assert Decimal(cash_entry["closing_debit"]) == Decimal("30000.00")
        assert Decimal(cash_entry["closing_credit"]) == Decimal("5000.00")

        # Revenue: CR 30000 from JE1
        assert b["revenue_id"] in accts
        assert Decimal(accts[b["revenue_id"]]["closing_credit"]) == Decimal("30000.00")
        assert Decimal(accts[b["revenue_id"]]["closing_debit"]) == Decimal("0.00")

        # Expense: DR 5000 from JE2
        assert b["expense_id"] in accts
        assert Decimal(accts[b["expense_id"]]["closing_debit"]) == Decimal("5000.00")
        assert Decimal(accts[b["expense_id"]]["closing_credit"]) == Decimal("0.00")

        # Company A accounts must NOT appear in Company B's TB
        assert a["cash_id"] not in accts
        assert a["revenue_id"] not in accts
        assert a["expense_id"] not in accts

    # -------------------------------------------------------------------
    # 7. Cross-company account usage rejected
    # -------------------------------------------------------------------

    def test_cross_company_account_rejected(self, fresh_db):
        """Using Company A's account in Company B's journal entry must be
        rejected by GL Validation Step 3 (Account-Company Affinity).

        The draft JE may be created (add-journal-entry only checks existence),
        but submit-journal-entry must fail because GL posting validates that
        each account belongs to the posting company."""
        conn = fresh_db
        envs = _setup_two_companies(conn)
        a = envs["a"]
        b = envs["b"]

        # Attempt to create a JE for Company B using Company A's cash account
        # add-journal-entry only checks account existence, not company affinity
        lines = json.dumps([
            {"account_id": a["cash_id"], "debit": "1000.00", "credit": "0"},
            {"account_id": b["revenue_id"], "debit": "0", "credit": "1000.00",
             "cost_center_id": b["cost_center_id"]},
        ])
        result = _call_action(
            "erpclaw-journals", "add-journal-entry", conn,
            company_id=b["company_id"], posting_date="2026-07-01", lines=lines,
        )
        assert result["status"] == "ok", \
            "Draft creation should succeed (no company check at draft stage)"
        je_id = result["journal_entry_id"]

        # Submit should FAIL: GL validation Step 3 rejects cross-company accounts
        result = _call_action(
            "erpclaw-journals", "submit-journal-entry", conn,
            journal_entry_id=je_id, company_id=b["company_id"],
        )
        assert result["status"] == "error", \
            "Submit must fail when using accounts from a different company"
        assert "step 3" in result.get("message", "").lower() or \
               "company" in result.get("message", "").lower(), \
            f"Error should mention company affinity, got: {result.get('message')}"

        # Verify no GL entries were created
        gl_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id = ?",
            (je_id,),
        ).fetchone()["cnt"]
        assert gl_count == 0, \
            "No GL entries should exist for the rejected journal entry"

    # -------------------------------------------------------------------
    # 8. Company-specific customers and suppliers
    # -------------------------------------------------------------------

    def test_company_specific_customers(self, fresh_db):
        """Customers created via add-customer belong to their specific company.
        Each company has its own customer list with no cross-contamination."""
        conn = fresh_db
        envs = _setup_two_companies(conn)
        a = envs["a"]
        b = envs["b"]

        # Create customers for Company A
        cust_a1 = _call_action(
            "erpclaw-selling", "add-customer", conn,
            name="Alpha Customer One", company_id=a["company_id"],
        )
        assert cust_a1["status"] == "ok"

        cust_a2 = _call_action(
            "erpclaw-selling", "add-customer", conn,
            name="Alpha Customer Two", company_id=a["company_id"],
        )
        assert cust_a2["status"] == "ok"

        # Create customers for Company B
        cust_b1 = _call_action(
            "erpclaw-selling", "add-customer", conn,
            name="Beta Customer One", company_id=b["company_id"],
        )
        assert cust_b1["status"] == "ok"

        # Create suppliers for each company
        supp_a = _call_action(
            "erpclaw-buying", "add-supplier", conn,
            name="Alpha Vendor", company_id=a["company_id"],
        )
        assert supp_a["status"] == "ok"

        supp_b = _call_action(
            "erpclaw-buying", "add-supplier", conn,
            name="Beta Vendor", company_id=b["company_id"],
        )
        assert supp_b["status"] == "ok"

        # Verify Company A has exactly 2 customers
        a_customers = conn.execute(
            "SELECT * FROM customer WHERE company_id = ?",
            (a["company_id"],),
        ).fetchall()
        assert len(a_customers) == 2
        a_cust_names = {r["name"] for r in a_customers}
        assert a_cust_names == {"Alpha Customer One", "Alpha Customer Two"}

        # Verify Company B has exactly 1 customer
        b_customers = conn.execute(
            "SELECT * FROM customer WHERE company_id = ?",
            (b["company_id"],),
        ).fetchall()
        assert len(b_customers) == 1
        assert b_customers[0]["name"] == "Beta Customer One"

        # Verify Company A has exactly 1 supplier
        a_suppliers = conn.execute(
            "SELECT * FROM supplier WHERE company_id = ?",
            (a["company_id"],),
        ).fetchall()
        assert len(a_suppliers) == 1
        assert a_suppliers[0]["name"] == "Alpha Vendor"

        # Verify Company B has exactly 1 supplier
        b_suppliers = conn.execute(
            "SELECT * FROM supplier WHERE company_id = ?",
            (b["company_id"],),
        ).fetchall()
        assert len(b_suppliers) == 1
        assert b_suppliers[0]["name"] == "Beta Vendor"

        # Cross-check: Company A customers are NOT in Company B and vice versa
        a_cust_ids = {r["id"] for r in a_customers}
        b_cust_ids = {r["id"] for r in b_customers}
        assert a_cust_ids.isdisjoint(b_cust_ids), \
            "Customer IDs must not overlap between companies"

        a_supp_ids = {r["id"] for r in a_suppliers}
        b_supp_ids = {r["id"] for r in b_suppliers}
        assert a_supp_ids.isdisjoint(b_supp_ids), \
            "Supplier IDs must not overlap between companies"
