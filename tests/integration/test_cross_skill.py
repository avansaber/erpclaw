"""Cross-skill integration tests (XS-01 through XS-10).

These tests verify that skills interact correctly when performing
multi-step workflows across skill boundaries.
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    create_test_company,
    create_test_fiscal_year,
    create_test_account,
    create_test_cost_center,
    create_test_customer,
    create_test_supplier,
    seed_naming_series,
)


# ---------------------------------------------------------------------------
# XS-01: JE submit posts GL entries
# ---------------------------------------------------------------------------

def test_XS01_je_submit_posts_gl(fresh_db):
    """Submitting a journal entry creates balanced GL entries."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    cash = create_test_account(conn, cid, "Cash", "asset",
                                account_type="bank", account_number="1001")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                   account_type="revenue", account_number="4001")
    cc = create_test_cost_center(conn, cid)

    lines = json.dumps([
        {"account_id": cash, "debit": "5000.00", "credit": "0"},
        {"account_id": revenue, "debit": "0", "credit": "5000.00", "cost_center_id": cc},
    ])

    # Create draft JE
    result = _call_action("erpclaw-journals", "add-journal-entry", conn,
                           company_id=cid, posting_date="2026-06-15", lines=lines)
    assert result["status"] == "ok"
    je_id = result["journal_entry_id"]

    # Submit JE
    result = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                           journal_entry_id=je_id)
    assert result["status"] == "ok"
    assert result["gl_entries_created"] == 2

    # Verify GL entries exist and are balanced
    gl_rows = conn.execute(
        "SELECT * FROM gl_entry WHERE voucher_type='journal_entry' AND voucher_id=? AND is_cancelled=0",
        (je_id,),
    ).fetchall()
    assert len(gl_rows) == 2

    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_debit == total_credit == Decimal("5000.00")


# ---------------------------------------------------------------------------
# XS-02: JE cancel reverses GL
# ---------------------------------------------------------------------------

def test_XS02_je_cancel_reverses_gl(fresh_db):
    """Cancelling a submitted JE marks original GL as cancelled and creates reversals."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    cash = create_test_account(conn, cid, "Cash", "asset",
                                account_type="bank", account_number="1001")
    expense = create_test_account(conn, cid, "Rent", "expense",
                                   account_type="expense", account_number="5001")
    cc = create_test_cost_center(conn, cid)

    lines = json.dumps([
        {"account_id": expense, "debit": "1200.00", "credit": "0", "cost_center_id": cc},
        {"account_id": cash, "debit": "0", "credit": "1200.00"},
    ])

    result = _call_action("erpclaw-journals", "add-journal-entry", conn,
                           company_id=cid, posting_date="2026-06-15", lines=lines)
    je_id = result["journal_entry_id"]

    _call_action("erpclaw-journals", "submit-journal-entry", conn,
                  journal_entry_id=je_id)

    # Cancel
    result = _call_action("erpclaw-journals", "cancel-journal-entry", conn,
                           journal_entry_id=je_id)
    assert result["status"] == "ok"

    # Original entries should be cancelled
    cancelled = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id=? AND is_cancelled=1",
        (je_id,),
    ).fetchone()["cnt"]
    assert cancelled == 2

    # Reversal entries should exist (not cancelled)
    reversals = conn.execute(
        "SELECT * FROM gl_entry WHERE voucher_id=? AND is_cancelled=0",
        (je_id,),
    ).fetchall()
    assert len(reversals) == 2

    # Reversals should also be balanced
    total_d = sum(Decimal(r["debit"]) for r in reversals)
    total_c = sum(Decimal(r["credit"]) for r in reversals)
    assert total_d == total_c


# ---------------------------------------------------------------------------
# XS-03: Payment submit posts GL
# ---------------------------------------------------------------------------

def test_XS03_payment_submit_posts_gl(fresh_db):
    """Submitting a payment creates GL entries (DR bank, CR receivable)."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    bank = create_test_account(conn, cid, "Bank", "asset",
                                account_type="bank", account_number="1010")
    receivable = create_test_account(conn, cid, "Accounts Receivable", "asset",
                                      account_type="receivable", account_number="1200")
    customer_id = create_test_customer(conn, cid)

    # Create draft payment (receive from customer)
    result = _call_action("erpclaw-payments", "add-payment", conn,
                           company_id=cid, payment_type="receive",
                           posting_date="2026-06-15",
                           party_type="customer", party_id=customer_id,
                           paid_from_account=receivable,
                           paid_to_account=bank,
                           paid_amount="3000.00")
    assert result["status"] == "ok"
    pe_id = result["payment_entry_id"]

    # Submit
    result = _call_action("erpclaw-payments", "submit-payment", conn,
                           payment_entry_id=pe_id)
    assert result["status"] == "ok"
    assert result["gl_entries_created"] == 2

    # Verify GL entries
    gl_rows = conn.execute(
        "SELECT * FROM gl_entry WHERE voucher_type='payment_entry' AND voucher_id=? AND is_cancelled=0",
        (pe_id,),
    ).fetchall()
    assert len(gl_rows) == 2

    total_d = sum(Decimal(r["debit"]) for r in gl_rows)
    total_c = sum(Decimal(r["credit"]) for r in gl_rows)
    assert total_d == total_c == Decimal("3000.00")


# ---------------------------------------------------------------------------
# XS-04: Payment submit creates PLE
# ---------------------------------------------------------------------------

def test_XS04_payment_submit_creates_ple(fresh_db):
    """Submitting a payment creates a payment_ledger_entry."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    bank = create_test_account(conn, cid, "Bank", "asset",
                                account_type="bank", account_number="1010")
    receivable = create_test_account(conn, cid, "AR", "asset",
                                      account_type="receivable", account_number="1200")
    customer_id = create_test_customer(conn, cid)

    result = _call_action("erpclaw-payments", "add-payment", conn,
                           company_id=cid, payment_type="receive",
                           posting_date="2026-06-15",
                           party_type="customer", party_id=customer_id,
                           paid_from_account=receivable,
                           paid_to_account=bank,
                           paid_amount="2500.00")
    pe_id = result["payment_entry_id"]

    _call_action("erpclaw-payments", "submit-payment", conn,
                  payment_entry_id=pe_id)

    # Verify PLE exists
    ple = conn.execute(
        "SELECT * FROM payment_ledger_entry WHERE voucher_type='payment_entry' AND voucher_id=?",
        (pe_id,),
    ).fetchall()
    assert len(ple) == 1
    assert ple[0]["party_type"] == "customer"
    assert ple[0]["party_id"] == customer_id
    # Receive payment: negative PLE (reduces receivable)
    assert Decimal(ple[0]["amount"]) == Decimal("-2500.00")


# ---------------------------------------------------------------------------
# XS-05: Payment cancel reverses all (GL + PLE)
# ---------------------------------------------------------------------------

def test_XS05_payment_cancel_reverses_all(fresh_db):
    """Cancelling a payment reverses GL entries and delinks PLE."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    bank = create_test_account(conn, cid, "Bank", "asset",
                                account_type="bank", account_number="1010")
    payable = create_test_account(conn, cid, "AP", "liability",
                                   account_type="payable", account_number="2100")
    supplier_id = create_test_supplier(conn, cid)

    result = _call_action("erpclaw-payments", "add-payment", conn,
                           company_id=cid, payment_type="pay",
                           posting_date="2026-06-15",
                           party_type="supplier", party_id=supplier_id,
                           paid_from_account=bank,
                           paid_to_account=payable,
                           paid_amount="1500.00")
    pe_id = result["payment_entry_id"]

    _call_action("erpclaw-payments", "submit-payment", conn,
                  payment_entry_id=pe_id)

    # Cancel
    result = _call_action("erpclaw-payments", "cancel-payment", conn,
                           payment_entry_id=pe_id)
    assert result["status"] == "ok"

    # GL: originals cancelled, reversals created
    cancelled_gl = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id=? AND is_cancelled=1",
        (pe_id,),
    ).fetchone()["cnt"]
    assert cancelled_gl == 2

    # PLE: original delinked, reversal created
    delinked = conn.execute(
        "SELECT COUNT(*) as cnt FROM payment_ledger_entry WHERE voucher_id=? AND delinked=1",
        (pe_id,),
    ).fetchone()["cnt"]
    assert delinked == 1

    # Net PLE should be zero
    net_ple = conn.execute(
        "SELECT COALESCE(SUM(CAST(amount AS REAL)),0) as net FROM payment_ledger_entry WHERE voucher_id=?",
        (pe_id,),
    ).fetchone()["net"]
    assert abs(net_ple) < 0.01


# ---------------------------------------------------------------------------
# XS-06: FY close zeros P&L accounts
# ---------------------------------------------------------------------------

def test_XS06_fy_close_zeros_pl_accounts(fresh_db):
    """Closing a fiscal year zeroes all income and expense accounts."""
    conn = fresh_db
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    revenue = create_test_account(conn, cid, "Revenue", "income",
                                   account_type="revenue", account_number="4001")
    expense = create_test_account(conn, cid, "Rent", "expense",
                                   account_type="expense", account_number="5001")
    retained = create_test_account(conn, cid, "Retained Earnings", "equity",
                                    account_type="equity", account_number="3200")
    cc = create_test_cost_center(conn, cid)

    # Post some income and expense via journals
    lines = json.dumps([
        {"account_id": revenue, "debit": "0", "credit": "10000.00", "cost_center_id": cc},
        {"account_id": expense, "debit": "4000.00", "credit": "0", "cost_center_id": cc},
        {"account_id": retained, "debit": "6000.00", "credit": "0"},
    ])
    result = _call_action("erpclaw-journals", "add-journal-entry", conn,
                           company_id=cid, posting_date="2026-06-15", lines=lines)
    je_id = result["journal_entry_id"]
    _call_action("erpclaw-journals", "submit-journal-entry", conn,
                  journal_entry_id=je_id)

    # Close fiscal year
    result = _call_action("erpclaw-gl", "close-fiscal-year", conn,
                           fiscal_year_id=fy_id,
                           closing_account_id=retained,
                           posting_date="2026-12-31")
    assert result["status"] == "ok"
    assert result["fiscal_year_closed"] is True

    # Check P&L accounts are zeroed: sum of all GL entries for each should net to zero
    for acct_id in (revenue, expense):
        bal = conn.execute(
            """SELECT COALESCE(SUM(CAST(debit AS REAL)),0) - COALESCE(SUM(CAST(credit AS REAL)),0) as net
               FROM gl_entry WHERE account_id = ? AND is_cancelled = 0""",
            (acct_id,),
        ).fetchone()["net"]
        assert abs(bal) < 0.01, f"P&L account {acct_id} not zeroed: {bal}"


# ---------------------------------------------------------------------------
# XS-07: FY close transfers to retained earnings
# ---------------------------------------------------------------------------

def test_XS07_fy_close_transfers_to_retained_earnings(fresh_db):
    """Closing a fiscal year transfers net P&L to retained earnings account."""
    conn = fresh_db
    cid = create_test_company(conn)
    fy_id = create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    cash = create_test_account(conn, cid, "Cash", "asset",
                                account_type="bank", account_number="1001")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                   account_type="revenue", account_number="4001")
    expense = create_test_account(conn, cid, "Rent", "expense",
                                   account_type="expense", account_number="5001")
    retained = create_test_account(conn, cid, "Retained Earnings", "equity",
                                    account_type="equity", account_number="3200")
    cc = create_test_cost_center(conn, cid)

    # Record revenue: DR Cash 8000, CR Revenue 8000
    lines1 = json.dumps([
        {"account_id": cash, "debit": "8000.00", "credit": "0"},
        {"account_id": revenue, "debit": "0", "credit": "8000.00", "cost_center_id": cc},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=cid, posting_date="2026-03-15", lines=lines1)
    _call_action("erpclaw-journals", "submit-journal-entry", conn,
                  journal_entry_id=r["journal_entry_id"])

    # Record expense: DR Expense 3000, CR Cash 3000
    lines2 = json.dumps([
        {"account_id": expense, "debit": "3000.00", "credit": "0", "cost_center_id": cc},
        {"account_id": cash, "debit": "0", "credit": "3000.00"},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=cid, posting_date="2026-04-15", lines=lines2)
    _call_action("erpclaw-journals", "submit-journal-entry", conn,
                  journal_entry_id=r["journal_entry_id"])

    # Net P&L = 8000 income - 3000 expense = 5000

    result = _call_action("erpclaw-gl", "close-fiscal-year", conn,
                           fiscal_year_id=fy_id,
                           closing_account_id=retained,
                           posting_date="2026-12-31")
    assert result["status"] == "ok"

    # Retained earnings should have net 5000 credit from closing entries
    closing_entries = conn.execute(
        """SELECT COALESCE(SUM(CAST(credit AS REAL)),0) - COALESCE(SUM(CAST(debit AS REAL)),0) as net
           FROM gl_entry WHERE account_id = ? AND voucher_type = 'period_closing' AND is_cancelled = 0""",
        (retained,),
    ).fetchone()["net"]
    assert abs(closing_entries - 5000.0) < 0.01


# ---------------------------------------------------------------------------
# XS-08: JE and PAY get different naming prefixes
# ---------------------------------------------------------------------------

def test_XS08_naming_prefix_separation(fresh_db):
    """Journal entries and payments use distinct naming series prefixes."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    cash = create_test_account(conn, cid, "Cash", "asset",
                                account_type="bank", account_number="1001")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                   account_type="revenue", account_number="4001")
    receivable = create_test_account(conn, cid, "AR", "asset",
                                      account_type="receivable", account_number="1200")
    cc = create_test_cost_center(conn, cid)
    customer_id = create_test_customer(conn, cid)

    # Create a journal entry
    lines = json.dumps([
        {"account_id": cash, "debit": "1000.00", "credit": "0"},
        {"account_id": revenue, "debit": "0", "credit": "1000.00", "cost_center_id": cc},
    ])
    je_result = _call_action("erpclaw-journals", "add-journal-entry", conn,
                              company_id=cid, posting_date="2026-06-15", lines=lines)
    je_naming = je_result["naming_series"]

    # Create a payment
    pay_result = _call_action("erpclaw-payments", "add-payment", conn,
                               company_id=cid, payment_type="receive",
                               posting_date="2026-06-15",
                               party_type="customer", party_id=customer_id,
                               paid_from_account=receivable,
                               paid_to_account=cash,
                               paid_amount="500.00")
    pay_naming = pay_result["naming_series"]

    # They must have different prefixes
    je_prefix = je_naming.rsplit("-", 1)[0]  # e.g., "JE-2026"
    pay_prefix = pay_naming.rsplit("-", 1)[0]  # e.g., "PAY-2026"
    assert je_prefix != pay_prefix, f"JE prefix '{je_prefix}' should differ from PAY prefix '{pay_prefix}'"

    # JE should start with JE-, payment with PAY-
    assert je_naming.startswith("JE-"), f"JE naming should start with 'JE-': {je_naming}"
    assert pay_naming.startswith("PAY-"), f"Payment naming should start with 'PAY-': {pay_naming}"


# ---------------------------------------------------------------------------
# XS-09: Full W1 setup workflow
# ---------------------------------------------------------------------------

def test_XS09_full_w1_setup_workflow(fresh_db):
    """The W1 day-one workflow: company + FY + CoA + naming series + defaults."""
    conn = fresh_db

    # Step 1: Create company via setup skill
    result = _call_action("erpclaw-setup", "setup-company", conn,
                           name="Acme Corp", abbr="AC",
                           currency="USD", country="United States",
                           fiscal_year_start_month="1")
    assert result["status"] == "ok"
    cid = result["company_id"]

    # Step 2: Create fiscal year via GL skill
    result = _call_action("erpclaw-gl", "add-fiscal-year", conn,
                           company_id=cid, name="FY 2026",
                           start_date="2026-01-01", end_date="2026-12-31")
    assert result["status"] == "ok"
    fy_id = result["fiscal_year_id"]

    # Step 3: Import chart of accounts via GL skill
    result = _call_action("erpclaw-gl", "setup-chart-of-accounts", conn,
                           company_id=cid, template="us_gaap")
    assert result["status"] == "ok"
    assert result["accounts_created"] > 50  # US GAAP has 94 accounts

    # Step 4: Seed naming series via GL skill
    result = _call_action("erpclaw-gl", "seed-naming-series", conn,
                           company_id=cid)
    assert result["status"] == "ok"
    assert result["series_created"] > 0

    # Step 5: Verify everything via GL status
    result = _call_action("erpclaw-gl", "status", conn, company_id=cid)
    assert result["status"] == "ok"
    assert result["accounts"] > 50
    assert result["fiscal_years"] == 1

    # Step 6: Verify reports skill can read the setup
    result = _call_action("erpclaw-reports", "trial-balance", conn,
                           company_id=cid, to_date="2026-12-31")
    assert result["status"] == "ok"
    # Empty TB — no GL entries yet
    assert result["total_debit"] == "0.00"
    assert result["total_credit"] == "0.00"


# ---------------------------------------------------------------------------
# XS-10: GL integrity after mixed operations
# ---------------------------------------------------------------------------

def test_XS10_gl_integrity_after_mixed_ops(fresh_db):
    """GL remains balanced after a sequence of submits and cancellations."""
    conn = fresh_db
    cid = create_test_company(conn)
    create_test_fiscal_year(conn, cid)
    seed_naming_series(conn, cid)

    cash = create_test_account(conn, cid, "Cash", "asset",
                                account_type="bank", account_number="1001")
    revenue = create_test_account(conn, cid, "Revenue", "income",
                                   account_type="revenue", account_number="4001")
    expense = create_test_account(conn, cid, "Rent", "expense",
                                   account_type="expense", account_number="5001")
    receivable = create_test_account(conn, cid, "AR", "asset",
                                      account_type="receivable", account_number="1200")
    cc = create_test_cost_center(conn, cid)
    customer_id = create_test_customer(conn, cid)

    je_ids = []
    pe_ids = []

    # Create and submit 5 journal entries
    for i in range(5):
        amount = str((i + 1) * 1000)
        lines = json.dumps([
            {"account_id": cash, "debit": amount, "credit": "0"},
            {"account_id": revenue, "debit": "0", "credit": amount, "cost_center_id": cc},
        ])
        r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                          company_id=cid, posting_date=f"2026-0{i+1}-15", lines=lines)
        je_id = r["journal_entry_id"]
        _call_action("erpclaw-journals", "submit-journal-entry", conn,
                      journal_entry_id=je_id)
        je_ids.append(je_id)

    # Create and submit 3 payments
    for i in range(3):
        amount = str((i + 1) * 500)
        r = _call_action("erpclaw-payments", "add-payment", conn,
                          company_id=cid, payment_type="receive",
                          posting_date=f"2026-0{i+1}-20",
                          party_type="customer", party_id=customer_id,
                          paid_from_account=receivable,
                          paid_to_account=cash,
                          paid_amount=amount)
        pe_id = r["payment_entry_id"]
        _call_action("erpclaw-payments", "submit-payment", conn,
                      payment_entry_id=pe_id)
        pe_ids.append(pe_id)

    # Cancel 2 JEs and 1 payment
    _call_action("erpclaw-journals", "cancel-journal-entry", conn,
                  journal_entry_id=je_ids[0])
    _call_action("erpclaw-journals", "cancel-journal-entry", conn,
                  journal_entry_id=je_ids[2])
    _call_action("erpclaw-payments", "cancel-payment", conn,
                  payment_entry_id=pe_ids[1])

    # Check GL integrity
    result = _call_action("erpclaw-gl", "check-gl-integrity", conn,
                           company_id=cid)
    assert result["status"] == "ok"
    assert result["balanced"] is True, f"GL not balanced: difference={result['difference']}"
