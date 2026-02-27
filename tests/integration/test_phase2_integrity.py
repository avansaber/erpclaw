"""Phase 2 cross-skill integration tests for GL integrity and mixed operations.

Tests XS-25 through XS-33 verify GL balance invariants, cancellation
symmetry, naming series separation, stock ledger consistency, and
payment ledger netting across selling, buying, journals, and payments.
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    setup_phase2_environment,
    create_test_item,
    seed_stock_for_item,
    create_test_warehouse,
    create_test_account,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _items_json(env, qty="10", rate="50.00"):
    """Build the items JSON payload used by selling/buying actions."""
    return json.dumps([{
        "item_id": env["item_id"],
        "qty": qty,
        "rate": rate,
        "warehouse_id": env["warehouse_id"],
    }])


def _setup_with_expense_default(conn):
    """Call setup_phase2_environment and also set default_expense_account_id
    on the company so that purchase invoice submit can find it."""
    env = setup_phase2_environment(conn)
    conn.execute(
        "UPDATE company SET default_expense_account_id = ? WHERE id = ?",
        (env["expense_id"], env["company_id"]),
    )
    conn.commit()
    return env


def _create_and_submit_sales_invoice(conn, env, qty="5", rate="100.00"):
    """Create a standalone sales invoice and submit it.
    Returns (sales_invoice_id, grand_total_decimal)."""
    items = json.dumps([{
        "item_id": env["item_id"],
        "qty": qty,
        "rate": rate,
        "warehouse_id": env["warehouse_id"],
    }])
    r = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                      company_id=env["company_id"],
                      customer_id=env["customer_id"],
                      posting_date="2026-06-15",
                      items=items)
    assert r["status"] == "ok", f"create-sales-invoice failed: {r}"
    si_id = r["sales_invoice_id"]
    grand = Decimal(r["grand_total"])

    r2 = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                       sales_invoice_id=si_id)
    assert r2["status"] == "ok", f"submit-sales-invoice failed: {r2}"
    return si_id, grand


def _create_and_submit_purchase_invoice(conn, env, qty="5", rate="80.00"):
    """Create a standalone purchase invoice and submit it.
    Returns (purchase_invoice_id, grand_total_decimal)."""
    items = json.dumps([{
        "item_id": env["item_id"],
        "qty": qty,
        "rate": rate,
        "warehouse_id": env["warehouse_id"],
    }])
    r = _call_action("erpclaw-buying", "create-purchase-invoice", conn,
                      company_id=env["company_id"],
                      supplier_id=env["supplier_id"],
                      posting_date="2026-06-15",
                      items=items)
    assert r["status"] == "ok", f"create-purchase-invoice failed: {r}"
    pi_id = r["purchase_invoice_id"]
    grand = Decimal(r["grand_total"])

    r2 = _call_action("erpclaw-buying", "submit-purchase-invoice", conn,
                       purchase_invoice_id=pi_id)
    assert r2["status"] == "ok", f"submit-purchase-invoice failed: {r2}"
    return pi_id, grand


def _create_and_submit_journal_entry(conn, env, amount="1000.00"):
    """Create and submit a JE (DR bank, CR income). Returns je_id."""
    lines = json.dumps([
        {"account_id": env["bank_id"], "debit": amount, "credit": "0"},
        {"account_id": env["income_id"], "debit": "0", "credit": amount,
         "cost_center_id": env["cost_center_id"]},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=env["company_id"],
                      posting_date="2026-06-15", lines=lines)
    assert r["status"] == "ok"
    je_id = r["journal_entry_id"]

    r2 = _call_action("erpclaw-journals", "submit-journal-entry", conn,
                       journal_entry_id=je_id)
    assert r2["status"] == "ok"
    return je_id


def _create_and_submit_payment(conn, env, amount="500.00"):
    """Create and submit a receive payment. Returns pe_id."""
    r = _call_action("erpclaw-payments", "add-payment", conn,
                      company_id=env["company_id"],
                      payment_type="receive",
                      posting_date="2026-06-15",
                      party_type="customer",
                      party_id=env["customer_id"],
                      paid_from_account=env["receivable_id"],
                      paid_to_account=env["bank_id"],
                      paid_amount=amount)
    assert r["status"] == "ok"
    pe_id = r["payment_entry_id"]

    r2 = _call_action("erpclaw-payments", "submit-payment", conn,
                       payment_entry_id=pe_id)
    assert r2["status"] == "ok"
    return pe_id


# ---------------------------------------------------------------------------
# XS-25: Mixed selling + buying GL balanced
# ---------------------------------------------------------------------------

def test_XS25_mixed_selling_buying_gl_balanced(fresh_db):
    """Create and submit 1 sales invoice + 1 purchase invoice + 1 JE + 1 payment.
    Verify total GL is balanced (SUM(debit) = SUM(credit) for non-cancelled)."""
    conn = fresh_db
    env = _setup_with_expense_default(conn)

    # 1. Sales invoice
    _create_and_submit_sales_invoice(conn, env)

    # 2. Purchase invoice
    _create_and_submit_purchase_invoice(conn, env)

    # 3. Journal entry
    _create_and_submit_journal_entry(conn, env)

    # 4. Payment
    _create_and_submit_payment(conn, env)

    # Verify GL balanced
    totals = conn.execute(
        """SELECT COALESCE(SUM(CAST(debit AS REAL)),0) as total_debit,
                  COALESCE(SUM(CAST(credit AS REAL)),0) as total_credit
           FROM gl_entry WHERE is_cancelled = 0"""
    ).fetchone()

    diff = abs(totals["total_debit"] - totals["total_credit"])
    assert diff < 0.01, (
        f"GL not balanced: debit={totals['total_debit']}, "
        f"credit={totals['total_credit']}, diff={diff}"
    )


# ---------------------------------------------------------------------------
# XS-26: Cancel and resubmit GL integrity
# ---------------------------------------------------------------------------

def test_XS26_cancel_and_resubmit_gl_integrity(fresh_db):
    """Submit invoice, cancel it, submit a new one. Verify: original GL
    cancelled, reversal GL created, new GL created, overall GL balanced."""
    conn = fresh_db
    env = _setup_with_expense_default(conn)

    # Submit first invoice
    si_id_1, _ = _create_and_submit_sales_invoice(conn, env, qty="3", rate="100.00")

    # Count original GL entries
    orig_gl_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_type='sales_invoice' "
        "AND voucher_id=? AND is_cancelled=0",
        (si_id_1,),
    ).fetchone()["cnt"]
    assert orig_gl_count >= 2, "Should have at least 2 GL entries for invoice"

    # Cancel the invoice
    r = _call_action("erpclaw-selling", "cancel-sales-invoice", conn,
                      sales_invoice_id=si_id_1)
    assert r["status"] == "ok"

    # Verify original GL entries are marked cancelled
    cancelled_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_type='sales_invoice' "
        "AND voucher_id=? AND is_cancelled=1",
        (si_id_1,),
    ).fetchone()["cnt"]
    assert cancelled_count > 0, "Original GL entries should be cancelled"

    # Verify reversal GL entries exist (non-cancelled for same voucher)
    reversal_count = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_type='sales_invoice' "
        "AND voucher_id=? AND is_cancelled=0",
        (si_id_1,),
    ).fetchone()["cnt"]
    assert reversal_count > 0, "Reversal GL entries should exist"

    # Submit a brand new invoice
    si_id_2, _ = _create_and_submit_sales_invoice(conn, env, qty="4", rate="75.00")

    # Verify new invoice has its own GL
    new_gl = conn.execute(
        "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_type='sales_invoice' "
        "AND voucher_id=? AND is_cancelled=0",
        (si_id_2,),
    ).fetchone()["cnt"]
    assert new_gl >= 2, "New invoice should have GL entries"

    # Overall GL must still be balanced
    totals = conn.execute(
        """SELECT COALESCE(SUM(CAST(debit AS REAL)),0) as total_debit,
                  COALESCE(SUM(CAST(credit AS REAL)),0) as total_credit
           FROM gl_entry WHERE is_cancelled = 0"""
    ).fetchone()
    diff = abs(totals["total_debit"] - totals["total_credit"])
    assert diff < 0.01, (
        f"GL not balanced after cancel+resubmit: diff={diff}"
    )


# ---------------------------------------------------------------------------
# XS-27: Invariant double-entry after 10+ mixed operations
# ---------------------------------------------------------------------------

def test_XS27_invariant_double_entry(fresh_db):
    """After 10+ mixed operations (selling + buying + journals + payments),
    check: SUM(debit) = SUM(credit) across ALL non-cancelled gl_entry rows."""
    conn = fresh_db
    env = _setup_with_expense_default(conn)

    # 3 sales invoices
    for i in range(3):
        _create_and_submit_sales_invoice(conn, env, qty=str(2 + i), rate="50.00")

    # 3 purchase invoices
    for i in range(3):
        _create_and_submit_purchase_invoice(conn, env, qty=str(2 + i), rate="40.00")

    # 3 journal entries
    for i in range(3):
        _create_and_submit_journal_entry(conn, env, amount=str((i + 1) * 500))

    # 3 payments
    for i in range(3):
        _create_and_submit_payment(conn, env, amount=str((i + 1) * 200))

    # Cancel 1 JE to add complexity (won't impact balance if done correctly)
    lines = json.dumps([
        {"account_id": env["bank_id"], "debit": "999.00", "credit": "0"},
        {"account_id": env["income_id"], "debit": "0", "credit": "999.00",
         "cost_center_id": env["cost_center_id"]},
    ])
    r = _call_action("erpclaw-journals", "add-journal-entry", conn,
                      company_id=env["company_id"],
                      posting_date="2026-06-15", lines=lines)
    je_cancel_id = r["journal_entry_id"]
    _call_action("erpclaw-journals", "submit-journal-entry", conn,
                  journal_entry_id=je_cancel_id)
    _call_action("erpclaw-journals", "cancel-journal-entry", conn,
                  journal_entry_id=je_cancel_id)

    # That's 13 operations total. Now verify the invariant.
    totals = conn.execute(
        """SELECT COALESCE(SUM(CAST(debit AS REAL)),0) as total_debit,
                  COALESCE(SUM(CAST(credit AS REAL)),0) as total_credit
           FROM gl_entry WHERE is_cancelled = 0"""
    ).fetchone()

    diff = abs(totals["total_debit"] - totals["total_credit"])
    assert diff < 0.01, (
        f"Double-entry violated after 13 ops: debit={totals['total_debit']}, "
        f"credit={totals['total_credit']}, diff={diff}"
    )


# ---------------------------------------------------------------------------
# XS-28: Invariant per-voucher balance
# ---------------------------------------------------------------------------

def test_XS28_invariant_per_voucher_balance(fresh_db):
    """After operations, verify every voucher_id has balanced entries:
    for each unique (voucher_type, voucher_id), SUM(debit) = SUM(credit)."""
    conn = fresh_db
    env = _setup_with_expense_default(conn)

    # Mixed operations
    _create_and_submit_sales_invoice(conn, env)
    _create_and_submit_purchase_invoice(conn, env)
    _create_and_submit_journal_entry(conn, env)
    _create_and_submit_payment(conn, env)

    vouchers = conn.execute(
        """SELECT voucher_type, voucher_id,
                  SUM(CAST(debit AS REAL)) as total_debit,
                  SUM(CAST(credit AS REAL)) as total_credit
           FROM gl_entry WHERE is_cancelled = 0
           GROUP BY voucher_type, voucher_id"""
    ).fetchall()

    assert len(vouchers) > 0, "Should have vouchers with GL entries"
    for v in vouchers:
        diff = abs(v["total_debit"] - v["total_credit"])
        assert diff < 0.01, (
            f"Voucher {v['voucher_type']}:{v['voucher_id']} not balanced: "
            f"debit={v['total_debit']}, credit={v['total_credit']}, diff={diff}"
        )


# ---------------------------------------------------------------------------
# XS-29: Invariant GL immutability
# ---------------------------------------------------------------------------

def test_XS29_invariant_gl_immutability(fresh_db):
    """After operations, verify no gl_entry has been modified:
    all created_at == updated_at (or updated_at is NULL).
    GL entries must never be mutated."""
    conn = fresh_db
    env = _setup_with_expense_default(conn)

    # Do a mix of operations including a cancellation
    _create_and_submit_sales_invoice(conn, env)
    _create_and_submit_purchase_invoice(conn, env)
    je_id = _create_and_submit_journal_entry(conn, env)
    _call_action("erpclaw-journals", "cancel-journal-entry", conn,
                  journal_entry_id=je_id)

    # Check gl_entry schema
    cols = conn.execute("PRAGMA table_info(gl_entry)").fetchall()
    col_names = [c["name"] for c in cols]

    if "updated_at" in col_names:
        # If the column exists, no row should have been mutated
        mutated = conn.execute(
            """SELECT COUNT(*) as cnt FROM gl_entry
               WHERE updated_at IS NOT NULL AND updated_at != created_at"""
        ).fetchone()["cnt"]
        assert mutated == 0, (
            f"{mutated} GL entries have updated_at != created_at (immutability violated)"
        )
    # If updated_at column doesn't exist at all, the invariant holds by design


# ---------------------------------------------------------------------------
# XS-30: Invariant cancellation symmetry
# ---------------------------------------------------------------------------

def test_XS30_invariant_cancellation_symmetry(fresh_db):
    """For every is_cancelled=1 GL entry, verify a matching is_cancelled=0
    reversal exists with swapped debit/credit amounts."""
    conn = fresh_db
    env = _setup_with_expense_default(conn)

    # Create and cancel operations to generate cancelled + reversal entries
    si_id, _ = _create_and_submit_sales_invoice(conn, env, qty="3", rate="100.00")
    _call_action("erpclaw-selling", "cancel-sales-invoice", conn,
                  sales_invoice_id=si_id)

    je_id = _create_and_submit_journal_entry(conn, env, amount="2000.00")
    _call_action("erpclaw-journals", "cancel-journal-entry", conn,
                  journal_entry_id=je_id)

    # Get all cancelled vouchers
    cancelled_vouchers = conn.execute(
        """SELECT voucher_type, voucher_id,
                  SUM(CAST(debit AS REAL)) as c_debit,
                  SUM(CAST(credit AS REAL)) as c_credit,
                  COUNT(*) as cnt
           FROM gl_entry WHERE is_cancelled = 1
           GROUP BY voucher_type, voucher_id"""
    ).fetchall()

    assert len(cancelled_vouchers) > 0, "Should have some cancelled entries"

    for cv in cancelled_vouchers:
        # There should be non-cancelled entries for the same voucher (reversals)
        reversals = conn.execute(
            """SELECT SUM(CAST(debit AS REAL)) as r_debit,
                      SUM(CAST(credit AS REAL)) as r_credit,
                      COUNT(*) as cnt
               FROM gl_entry WHERE voucher_type = ? AND voucher_id = ?
                 AND is_cancelled = 0""",
            (cv["voucher_type"], cv["voucher_id"]),
        ).fetchone()

        assert reversals["cnt"] > 0, (
            f"No reversals found for cancelled {cv['voucher_type']}:{cv['voucher_id']}"
        )

        # The reversal entries themselves should be balanced
        r_diff = abs(reversals["r_debit"] - reversals["r_credit"])
        assert r_diff < 0.01, (
            f"Reversals for {cv['voucher_type']}:{cv['voucher_id']} not balanced: "
            f"debit={reversals['r_debit']}, credit={reversals['r_credit']}"
        )

        # The cancelled debit/credit should match the reversal credit/debit
        # (i.e., debit on cancelled = credit on reversal and vice versa)
        assert abs(cv["c_debit"] - reversals["r_credit"]) < 0.01, (
            f"Cancelled debit {cv['c_debit']} != reversal credit {reversals['r_credit']}"
        )
        assert abs(cv["c_credit"] - reversals["r_debit"]) < 0.01, (
            f"Cancelled credit {cv['c_credit']} != reversal debit {reversals['r_debit']}"
        )


# ---------------------------------------------------------------------------
# XS-31: Naming series Phase 2 separation
# ---------------------------------------------------------------------------

def test_XS31_naming_series_phase2_separation(fresh_db):
    """Create a sales order, purchase order, and journal entry.
    Verify each gets a distinct naming prefix (SO- vs PO- vs JE-)."""
    conn = fresh_db
    env = _setup_with_expense_default(conn)

    items = _items_json(env)

    # 1. Sales order
    so_r = _call_action("erpclaw-selling", "add-sales-order", conn,
                         company_id=env["company_id"],
                         customer_id=env["customer_id"],
                         posting_date="2026-06-15",
                         delivery_date="2026-07-15",
                         items=items)
    assert so_r["status"] == "ok"
    so_id = so_r["sales_order_id"]

    so_submit = _call_action("erpclaw-selling", "submit-sales-order", conn,
                              sales_order_id=so_id)
    assert so_submit["status"] == "ok"
    so_naming = so_submit["naming_series"]

    # 2. Purchase order
    po_r = _call_action("erpclaw-buying", "add-purchase-order", conn,
                         company_id=env["company_id"],
                         supplier_id=env["supplier_id"],
                         posting_date="2026-06-15",
                         items=items)
    assert po_r["status"] == "ok"
    po_id = po_r["purchase_order_id"]

    po_submit = _call_action("erpclaw-buying", "submit-purchase-order", conn,
                              purchase_order_id=po_id)
    assert po_submit["status"] == "ok"
    po_naming = po_submit["naming_series"]

    # 3. Journal entry
    je_id = _create_and_submit_journal_entry(conn, env)
    je_row = conn.execute(
        "SELECT naming_series FROM journal_entry WHERE id = ?", (je_id,)
    ).fetchone()
    je_naming = je_row["naming_series"]

    # Verify distinct prefixes
    assert so_naming.startswith("SO-"), f"SO naming should start with 'SO-': {so_naming}"
    assert po_naming.startswith("PO-"), f"PO naming should start with 'PO-': {po_naming}"
    assert je_naming.startswith("JE-"), f"JE naming should start with 'JE-': {je_naming}"

    # Extract prefix (everything before last dash-number)
    so_prefix = so_naming.rsplit("-", 1)[0]
    po_prefix = po_naming.rsplit("-", 1)[0]
    je_prefix = je_naming.rsplit("-", 1)[0]

    prefixes = {so_prefix, po_prefix, je_prefix}
    assert len(prefixes) == 3, (
        f"All naming prefixes must be distinct: SO={so_prefix}, PO={po_prefix}, JE={je_prefix}"
    )


# ---------------------------------------------------------------------------
# XS-32: Stock ledger consistency
# ---------------------------------------------------------------------------

def test_XS32_stock_ledger_consistency(fresh_db):
    """After delivery note submit (stock out) and purchase receipt submit
    (stock in), verify SLE entries exist with correct directions
    (negative for out, positive for in)."""
    conn = fresh_db
    env = _setup_with_expense_default(conn)

    items = _items_json(env, qty="5", rate="50.00")

    # 1. Create and submit a sales order, then delivery note (stock out)
    so_r = _call_action("erpclaw-selling", "add-sales-order", conn,
                         company_id=env["company_id"],
                         customer_id=env["customer_id"],
                         posting_date="2026-06-15",
                         delivery_date="2026-07-15",
                         items=items)
    assert so_r["status"] == "ok"
    so_id = so_r["sales_order_id"]

    _call_action("erpclaw-selling", "submit-sales-order", conn,
                  sales_order_id=so_id)

    dn_r = _call_action("erpclaw-selling", "create-delivery-note", conn,
                          sales_order_id=so_id,
                          posting_date="2026-06-16")
    assert dn_r["status"] == "ok"
    dn_id = dn_r["delivery_note_id"]

    dn_submit = _call_action("erpclaw-selling", "submit-delivery-note", conn,
                               delivery_note_id=dn_id)
    assert dn_submit["status"] == "ok"

    # Check SLE for delivery note: should be negative (stock out)
    dn_sles = conn.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type='delivery_note' AND voucher_id=? AND is_cancelled=0""",
        (dn_id,),
    ).fetchall()
    assert len(dn_sles) > 0, "Delivery note should create SLE entries"
    for sle in dn_sles:
        actual_qty = float(sle["actual_qty"])
        assert actual_qty < 0, (
            f"Delivery note SLE should be negative (stock out), got {actual_qty}"
        )

    # 2. Create and submit a purchase order, then purchase receipt (stock in)
    po_r = _call_action("erpclaw-buying", "add-purchase-order", conn,
                         company_id=env["company_id"],
                         supplier_id=env["supplier_id"],
                         posting_date="2026-06-15",
                         items=items)
    assert po_r["status"] == "ok"
    po_id = po_r["purchase_order_id"]

    _call_action("erpclaw-buying", "submit-purchase-order", conn,
                  purchase_order_id=po_id)

    pr_r = _call_action("erpclaw-buying", "create-purchase-receipt", conn,
                          purchase_order_id=po_id,
                          posting_date="2026-06-17")
    assert pr_r["status"] == "ok"
    pr_id = pr_r["purchase_receipt_id"]

    pr_submit = _call_action("erpclaw-buying", "submit-purchase-receipt", conn,
                               purchase_receipt_id=pr_id)
    assert pr_submit["status"] == "ok"

    # Check SLE for purchase receipt: should be positive (stock in)
    pr_sles = conn.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type='purchase_receipt' AND voucher_id=? AND is_cancelled=0""",
        (pr_id,),
    ).fetchall()
    assert len(pr_sles) > 0, "Purchase receipt should create SLE entries"
    for sle in pr_sles:
        actual_qty = float(sle["actual_qty"])
        assert actual_qty > 0, (
            f"Purchase receipt SLE should be positive (stock in), got {actual_qty}"
        )


# ---------------------------------------------------------------------------
# XS-33: Payment ledger entry netting
# ---------------------------------------------------------------------------

def test_XS33_payment_ledger_entry_netting(fresh_db):
    """Submit an invoice, then a full payment against it. Verify: 2 PLE rows
    exist (one for invoice, one for payment), they net to 0 or close to 0."""
    conn = fresh_db
    env = _setup_with_expense_default(conn)

    # Submit a sales invoice
    si_id, grand_total = _create_and_submit_sales_invoice(
        conn, env, qty="5", rate="100.00")

    # The invoice PLE should be positive (customer owes us)
    inv_ple = conn.execute(
        """SELECT * FROM payment_ledger_entry
           WHERE voucher_type='sales_invoice' AND voucher_id=?""",
        (si_id,),
    ).fetchall()
    assert len(inv_ple) == 1, f"Expected 1 PLE for invoice, got {len(inv_ple)}"
    inv_ple_amount = Decimal(inv_ple[0]["amount"])
    assert inv_ple_amount > 0, (
        f"Invoice PLE should be positive, got {inv_ple_amount}"
    )

    # Submit a payment for the full grand_total
    pe_id = _create_and_submit_payment(conn, env, amount=str(grand_total))

    # The payment PLE should be negative (reduces outstanding)
    pay_ple = conn.execute(
        """SELECT * FROM payment_ledger_entry
           WHERE voucher_type='payment_entry' AND voucher_id=?""",
        (pe_id,),
    ).fetchall()
    assert len(pay_ple) == 1, f"Expected 1 PLE for payment, got {len(pay_ple)}"
    pay_ple_amount = Decimal(pay_ple[0]["amount"])
    assert pay_ple_amount < 0, (
        f"Payment PLE should be negative, got {pay_ple_amount}"
    )

    # Net PLE for this customer should be zero (or very close)
    net_ple = conn.execute(
        """SELECT COALESCE(SUM(CAST(amount AS REAL)),0) as net
           FROM payment_ledger_entry
           WHERE party_type='customer' AND party_id=? AND delinked=0""",
        (env["customer_id"],),
    ).fetchone()["net"]
    assert abs(net_ple) < 0.01, (
        f"Net PLE for customer should be ~0 after full payment, got {net_ple}"
    )
