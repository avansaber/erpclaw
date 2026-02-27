"""Tests for intercompany elimination rules (V5).

10 tests:
- Create elimination rule (1)
- Run elimination creates balanced GL (1)
- Run elimination zeros IC revenue (1)
- Run elimination zeros IC expense (1)
- Idempotent re-run (1)
- Partial elimination (only matching rules) (1)
- List elimination entries audit trail (1)
- Consolidated trial balance after elimination (1)
- Same-company rejection (1)
- GL invariant: total debits = total credits (1)
"""
import uuid

import db_query
from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_cost_center,
    create_test_gl_entry,
)
from decimal import Decimal
from erpclaw_lib.decimal_utils import to_decimal


# ---------------------------------------------------------------------------
# Intercompany environment setup
# ---------------------------------------------------------------------------

def _setup_ic_environment(conn):
    """Create two companies with accounts and IC GL entries.

    Source company (seller): has income from IC sales
    Target company (buyer): has expense from IC purchases

    Returns dict with all IDs.
    """
    # Source company (seller)
    src_company = create_test_company(conn, "Source Corp", "SRC")
    src_fy = create_test_fiscal_year(conn, src_company, "FY 2026 SRC")

    src_income = create_test_account(
        conn, src_company, "IC Revenue", "income",
        account_type="revenue", account_number="S4000",
    )
    src_receivable = create_test_account(
        conn, src_company, "Accounts Receivable", "asset",
        account_type="receivable", account_number="S1200",
    )

    # Target company (buyer)
    tgt_company = create_test_company(conn, "Target Corp", "TGT")
    tgt_fy = create_test_fiscal_year(conn, tgt_company, "FY 2026 TGT")

    tgt_expense = create_test_account(
        conn, tgt_company, "IC Expense", "expense",
        account_type=None, account_number="T6000",
    )
    tgt_payable = create_test_account(
        conn, tgt_company, "Accounts Payable", "liability",
        account_type="payable", account_number="T2000",
    )

    # Simulate IC transaction GL entries:
    # Source company: DR Receivable 1000, CR Income 1000
    voucher_id = str(uuid.uuid4())
    create_test_gl_entry(
        conn, src_receivable, "2026-06-15", "1000.00", "0",
        voucher_type="sales_invoice", voucher_id=voucher_id,
    )
    create_test_gl_entry(
        conn, src_income, "2026-06-15", "0", "1000.00",
        voucher_type="sales_invoice", voucher_id=voucher_id,
    )

    # Target company: DR Expense 1000, CR Payable 1000
    pi_voucher_id = str(uuid.uuid4())
    create_test_gl_entry(
        conn, tgt_expense, "2026-06-15", "1000.00", "0",
        voucher_type="purchase_invoice", voucher_id=pi_voucher_id,
    )
    create_test_gl_entry(
        conn, tgt_payable, "2026-06-15", "0", "1000.00",
        voucher_type="purchase_invoice", voucher_id=pi_voucher_id,
    )

    return {
        "src_company": src_company,
        "src_fy": src_fy,
        "src_income": src_income,
        "src_receivable": src_receivable,
        "tgt_company": tgt_company,
        "tgt_fy": tgt_fy,
        "tgt_expense": tgt_expense,
        "tgt_payable": tgt_payable,
    }


# ---------------------------------------------------------------------------
# 1. Create elimination rule
# ---------------------------------------------------------------------------

def test_create_elimination_rule(fresh_db):
    env = _setup_ic_environment(fresh_db)

    r = _call_action(
        db_query.ACTIONS["add-elimination-rule"], fresh_db,
        name="IC Revenue/Expense Elimination",
        company_id=env["src_company"],
        target_company_id=env["tgt_company"],
        source_account_id=env["src_income"],
        target_account_id=env["tgt_expense"],
    )
    assert r["status"] == "ok"
    assert r["rule_id"]
    assert r["name"] == "IC Revenue/Expense Elimination"

    # Verify in DB
    rule = fresh_db.execute(
        "SELECT * FROM elimination_rule WHERE id = ?", (r["rule_id"],)
    ).fetchone()
    assert rule is not None
    assert rule["status"] == "active"


# ---------------------------------------------------------------------------
# 2. Run elimination creates balanced GL
# ---------------------------------------------------------------------------

def test_run_creates_balanced_gl(fresh_db):
    env = _setup_ic_environment(fresh_db)

    # Create rule
    _call_action(
        db_query.ACTIONS["add-elimination-rule"], fresh_db,
        name="IC Elim",
        company_id=env["src_company"],
        target_company_id=env["tgt_company"],
        source_account_id=env["src_income"],
        target_account_id=env["tgt_expense"],
    )

    # Run elimination
    r = _call_action(
        db_query.ACTIONS["run-elimination"], fresh_db,
        fiscal_year_id=env["src_fy"],
        posting_date="2026-12-31",
    )
    assert r["status"] == "ok"
    assert r["total_eliminated"] == 1
    assert to_decimal(r["eliminations"][0]["amount"]) == Decimal("1000.00")

    # Verify GL entries are balanced (DR = CR for elimination voucher)
    voucher_id = None
    elim_gl = fresh_db.execute(
        """SELECT * FROM gl_entry WHERE voucher_type = 'elimination_entry'
           AND is_cancelled = 0"""
    ).fetchall()
    assert len(elim_gl) == 2

    total_dr = sum(to_decimal(g["debit"]) for g in elim_gl)
    total_cr = sum(to_decimal(g["credit"]) for g in elim_gl)
    assert total_dr == total_cr == Decimal("1000.00")


# ---------------------------------------------------------------------------
# 3. Run elimination zeros IC revenue
# ---------------------------------------------------------------------------

def test_zeros_ic_revenue(fresh_db):
    env = _setup_ic_environment(fresh_db)

    _call_action(
        db_query.ACTIONS["add-elimination-rule"], fresh_db,
        name="IC Elim",
        company_id=env["src_company"],
        target_company_id=env["tgt_company"],
        source_account_id=env["src_income"],
        target_account_id=env["tgt_expense"],
    )

    _call_action(
        db_query.ACTIONS["run-elimination"], fresh_db,
        fiscal_year_id=env["src_fy"],
        posting_date="2026-12-31",
    )

    # Net income in source account should be zero after elimination
    # Original: CR 1000 (income). Elimination: DR 1000.
    net = fresh_db.execute(
        """SELECT COALESCE(decimal_sum(credit), '0') as c,
                  COALESCE(decimal_sum(debit), '0') as d
           FROM gl_entry WHERE account_id = ? AND is_cancelled = 0""",
        (env["src_income"],),
    ).fetchone()
    assert to_decimal(net["c"]) - to_decimal(net["d"]) == Decimal("0")


# ---------------------------------------------------------------------------
# 4. Run elimination zeros IC expense
# ---------------------------------------------------------------------------

def test_zeros_ic_expense(fresh_db):
    env = _setup_ic_environment(fresh_db)

    _call_action(
        db_query.ACTIONS["add-elimination-rule"], fresh_db,
        name="IC Elim",
        company_id=env["src_company"],
        target_company_id=env["tgt_company"],
        source_account_id=env["src_income"],
        target_account_id=env["tgt_expense"],
    )

    _call_action(
        db_query.ACTIONS["run-elimination"], fresh_db,
        fiscal_year_id=env["src_fy"],
        posting_date="2026-12-31",
    )

    # Net expense in target account should be zero after elimination
    # Original: DR 1000 (expense). Elimination: CR 1000.
    net = fresh_db.execute(
        """SELECT COALESCE(decimal_sum(debit), '0') as d,
                  COALESCE(decimal_sum(credit), '0') as c
           FROM gl_entry WHERE account_id = ? AND is_cancelled = 0""",
        (env["tgt_expense"],),
    ).fetchone()
    assert to_decimal(net["d"]) - to_decimal(net["c"]) == Decimal("0")


# ---------------------------------------------------------------------------
# 5. Idempotent re-run
# ---------------------------------------------------------------------------

def test_idempotent_rerun(fresh_db):
    env = _setup_ic_environment(fresh_db)

    _call_action(
        db_query.ACTIONS["add-elimination-rule"], fresh_db,
        name="IC Elim",
        company_id=env["src_company"],
        target_company_id=env["tgt_company"],
        source_account_id=env["src_income"],
        target_account_id=env["tgt_expense"],
    )

    # First run
    r1 = _call_action(
        db_query.ACTIONS["run-elimination"], fresh_db,
        fiscal_year_id=env["src_fy"],
        posting_date="2026-12-31",
    )
    assert r1["total_eliminated"] == 1

    # Second run — should create 0 new entries
    r2 = _call_action(
        db_query.ACTIONS["run-elimination"], fresh_db,
        fiscal_year_id=env["src_fy"],
        posting_date="2026-12-31",
    )
    assert r2["total_eliminated"] == 0

    # Still only 2 elimination GL entries total
    count = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_type = 'elimination_entry'"
    ).fetchone()["cnt"]
    assert count == 2


# ---------------------------------------------------------------------------
# 6. Partial elimination (only matching rules fire)
# ---------------------------------------------------------------------------

def test_partial_elimination(fresh_db):
    env = _setup_ic_environment(fresh_db)

    # Create a rule for a DIFFERENT account (no matching GL)
    other_income = create_test_account(
        fresh_db, env["src_company"], "Other Income", "income",
        account_type="revenue", account_number="S4500",
    )
    _call_action(
        db_query.ACTIONS["add-elimination-rule"], fresh_db,
        name="No-Match Rule",
        company_id=env["src_company"],
        target_company_id=env["tgt_company"],
        source_account_id=other_income,
        target_account_id=env["tgt_expense"],
    )

    # Also create the real rule
    _call_action(
        db_query.ACTIONS["add-elimination-rule"], fresh_db,
        name="Real Rule",
        company_id=env["src_company"],
        target_company_id=env["tgt_company"],
        source_account_id=env["src_income"],
        target_account_id=env["tgt_expense"],
    )

    r = _call_action(
        db_query.ACTIONS["run-elimination"], fresh_db,
        fiscal_year_id=env["src_fy"],
        posting_date="2026-12-31",
    )
    # Only the matching rule should fire
    assert r["total_eliminated"] == 1
    assert r["eliminations"][0]["rule_name"] == "Real Rule"


# ---------------------------------------------------------------------------
# 7. List elimination entries (audit trail)
# ---------------------------------------------------------------------------

def test_list_elimination_entries(fresh_db):
    env = _setup_ic_environment(fresh_db)

    _call_action(
        db_query.ACTIONS["add-elimination-rule"], fresh_db,
        name="IC Elim",
        company_id=env["src_company"],
        target_company_id=env["tgt_company"],
        source_account_id=env["src_income"],
        target_account_id=env["tgt_expense"],
    )

    _call_action(
        db_query.ACTIONS["run-elimination"], fresh_db,
        fiscal_year_id=env["src_fy"],
        posting_date="2026-12-31",
    )

    r = _call_action(
        db_query.ACTIONS["list-elimination-entries"], fresh_db,
        fiscal_year_id=env["src_fy"],
    )
    assert r["status"] == "ok"
    assert r["total"] == 1
    entry = r["entries"][0]
    assert entry["rule_name"] == "IC Elim"
    assert to_decimal(entry["amount"]) == Decimal("1000.00")
    assert entry["status"] == "posted"
    assert entry["source_company"] == "Source Corp"
    assert entry["target_company"] == "Target Corp"


# ---------------------------------------------------------------------------
# 8. Consolidated trial balance after elimination
# ---------------------------------------------------------------------------

def test_consolidated_tb_after_elimination(fresh_db):
    env = _setup_ic_environment(fresh_db)

    _call_action(
        db_query.ACTIONS["add-elimination-rule"], fresh_db,
        name="IC Elim",
        company_id=env["src_company"],
        target_company_id=env["tgt_company"],
        source_account_id=env["src_income"],
        target_account_id=env["tgt_expense"],
    )

    _call_action(
        db_query.ACTIONS["run-elimination"], fresh_db,
        fiscal_year_id=env["src_fy"],
        posting_date="2026-12-31",
    )

    # Run TB for source company — income should show zero net
    tb_src = _call_action(
        db_query.ACTIONS["trial-balance"], fresh_db,
        company_id=env["src_company"],
        to_date="2026-12-31",
    )
    assert tb_src["status"] == "ok"
    # Source income account: CR 1000 + DR 1000 = net 0 (may not appear if zero)
    income_accts = [a for a in tb_src["accounts"] if a["account_id"] == env["src_income"]]
    if income_accts:
        acct = income_accts[0]
        assert to_decimal(acct["closing_debit"]) == to_decimal(acct["closing_credit"])

    # Run TB for target company — expense should show zero net
    tb_tgt = _call_action(
        db_query.ACTIONS["trial-balance"], fresh_db,
        company_id=env["tgt_company"],
        to_date="2026-12-31",
    )
    assert tb_tgt["status"] == "ok"
    expense_accts = [a for a in tb_tgt["accounts"] if a["account_id"] == env["tgt_expense"]]
    if expense_accts:
        acct = expense_accts[0]
        assert to_decimal(acct["closing_debit"]) == to_decimal(acct["closing_credit"])


# ---------------------------------------------------------------------------
# 9. Same-company rejection
# ---------------------------------------------------------------------------

def test_same_company_rejection(fresh_db):
    env = _setup_ic_environment(fresh_db)

    r = _call_action(
        db_query.ACTIONS["add-elimination-rule"], fresh_db,
        name="Bad Rule",
        company_id=env["src_company"],
        target_company_id=env["src_company"],  # Same company!
        source_account_id=env["src_income"],
        target_account_id=env["src_income"],
    )
    assert r["status"] == "error"
    assert "different" in r["message"].lower()


# ---------------------------------------------------------------------------
# 10. GL invariant: total debits = total credits
# ---------------------------------------------------------------------------

def test_gl_invariant_after_elimination(fresh_db):
    env = _setup_ic_environment(fresh_db)

    _call_action(
        db_query.ACTIONS["add-elimination-rule"], fresh_db,
        name="IC Elim",
        company_id=env["src_company"],
        target_company_id=env["tgt_company"],
        source_account_id=env["src_income"],
        target_account_id=env["tgt_expense"],
    )

    _call_action(
        db_query.ACTIONS["run-elimination"], fresh_db,
        fiscal_year_id=env["src_fy"],
        posting_date="2026-12-31",
    )

    # Global GL invariant: total debits = total credits
    totals = fresh_db.execute(
        """SELECT decimal_sum(debit) as total_debit,
                  decimal_sum(credit) as total_credit
           FROM gl_entry WHERE is_cancelled = 0"""
    ).fetchone()
    assert to_decimal(totals["total_debit"]) == to_decimal(totals["total_credit"])
