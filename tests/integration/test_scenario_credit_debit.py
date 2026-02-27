"""Credit note and debit note integration tests.

Tests the full credit/debit note lifecycle across erpclaw-selling,
erpclaw-buying, erpclaw-payments, erpclaw-gl, and erpclaw-reports skills.
Verifies GL reversal, outstanding reduction, and trial balance integrity
after return documents are processed.
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    setup_phase2_environment,
    create_test_item,
    seed_stock_for_item,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _items_json(env, qty="10", rate="100.00"):
    """Build the standard selling items JSON payload."""
    return json.dumps([{
        "item_id": env["item_id"],
        "qty": qty,
        "rate": rate,
        "warehouse_id": env["warehouse_id"],
    }])


def _buying_items_json(env, qty="10", rate="50.00"):
    """Build the standard buying items JSON payload."""
    return json.dumps([{
        "item_id": env["item_id"],
        "qty": qty,
        "rate": rate,
        "warehouse_id": env["warehouse_id"],
    }])


def _set_default_expense_account(conn, company_id, expense_account_id):
    """Set the default expense account on the company."""
    conn.execute(
        "UPDATE company SET default_expense_account_id = ? WHERE id = ?",
        (expense_account_id, company_id),
    )
    conn.commit()


def _create_and_submit_sales_invoice(conn, env, qty="10", rate="100.00"):
    """Create a standalone sales invoice and submit it.
    Returns (invoice_id, grand_total)."""
    items_j = _items_json(env, qty=qty, rate=rate)

    r = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                      customer_id=env["customer_id"],
                      posting_date="2026-06-01",
                      items=items_j,
                      company_id=env["company_id"])
    assert r["status"] == "ok", f"create-sales-invoice failed: {r}"
    si_id = r["sales_invoice_id"]
    grand_total = r["grand_total"]

    r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                      sales_invoice_id=si_id)
    assert r["status"] == "ok", f"submit-sales-invoice failed: {r}"
    return si_id, grand_total


def _create_and_submit_purchase_invoice(conn, env, qty="10", rate="50.00"):
    """Create a standalone purchase invoice and submit it.
    Returns (invoice_id, grand_total)."""
    items_j = _buying_items_json(env, qty=qty, rate=rate)

    r = _call_action("erpclaw-buying", "create-purchase-invoice", conn,
                      supplier_id=env["supplier_id"],
                      company_id=env["company_id"],
                      items=items_j,
                      posting_date="2026-06-01",
                      due_date="2026-07-01")
    assert r["status"] == "ok", f"create-purchase-invoice failed: {r}"
    pi_id = r["purchase_invoice_id"]
    grand_total = r["grand_total"]

    r = _call_action("erpclaw-buying", "submit-purchase-invoice", conn,
                      purchase_invoice_id=pi_id)
    assert r["status"] == "ok", f"submit-purchase-invoice failed: {r}"
    return pi_id, grand_total


class TestCreditDebitScenario:
    """Integration tests for credit notes and debit notes across skills."""

    # -------------------------------------------------------------------
    # 1. Full credit note cycle: SO -> SI -> credit note -> GL reversal
    # -------------------------------------------------------------------

    def test_full_credit_note_cycle(self, fresh_db):
        """Full cycle: create SO -> submit SO -> create SI -> submit SI
        -> create credit note -> submit credit note -> verify GL reversal
        and outstanding reduction."""
        conn = fresh_db
        env = setup_phase2_environment(conn)

        items_j = _items_json(env, qty="5", rate="100.00")  # net = 500.00

        # Step 1: Create and submit sales order
        r = _call_action("erpclaw-selling", "add-sales-order", conn,
                          customer_id=env["customer_id"],
                          posting_date="2026-06-01",
                          items=items_j,
                          company_id=env["company_id"])
        assert r["status"] == "ok"
        so_id = r["sales_order_id"]

        r = _call_action("erpclaw-selling", "submit-sales-order", conn,
                          sales_order_id=so_id)
        assert r["status"] == "ok"

        # Step 2: Create and submit sales invoice from SO
        r = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                          sales_order_id=so_id,
                          posting_date="2026-06-05",
                          company_id=env["company_id"])
        assert r["status"] == "ok"
        si_id = r["sales_invoice_id"]

        r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                          sales_invoice_id=si_id)
        assert r["status"] == "ok"

        # Verify invoice outstanding
        si = conn.execute("SELECT * FROM sales_invoice WHERE id = ?",
                          (si_id,)).fetchone()
        original_outstanding = Decimal(si["outstanding_amount"])
        assert original_outstanding == Decimal("500.00")

        # Step 3: Create credit note for full amount
        cn_items = json.dumps([{
            "item_id": env["item_id"],
            "qty": "5",
            "rate": "100.00",
        }])
        r = _call_action("erpclaw-selling", "create-credit-note", conn,
                          against_invoice_id=si_id,
                          items=cn_items,
                          posting_date="2026-06-10",
                          reason="Customer returned goods")
        assert r["status"] == "ok"
        cn_id = r["credit_note_id"]

        # Verify credit note is stored as a return invoice
        cn = conn.execute("SELECT * FROM sales_invoice WHERE id = ?",
                          (cn_id,)).fetchone()
        assert cn["is_return"] == 1
        assert cn["return_against"] == si_id
        assert Decimal(cn["grand_total"]) == Decimal("-500.00")

        # Step 4: Submit credit note (uses submit-sales-invoice)
        r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                          sales_invoice_id=cn_id)
        assert r["status"] == "ok"

        # Step 5: Verify credit note GL entries
        cn_gl = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'credit_note' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (cn_id,),
        ).fetchall()
        assert len(cn_gl) >= 2, f"Expected >= 2 GL entries for credit note, got {len(cn_gl)}"

        # GL should be balanced
        cn_debit = sum(Decimal(g["debit"]) for g in cn_gl)
        cn_credit = sum(Decimal(g["credit"]) for g in cn_gl)
        assert abs(cn_debit - cn_credit) < Decimal("0.01"), (
            f"Credit note GL not balanced: D={cn_debit}, C={cn_credit}"
        )

        # Step 6: Update original invoice outstanding
        _call_action("erpclaw-selling", "update-invoice-outstanding", conn,
                      sales_invoice_id=si_id,
                      amount="500.00")

        si_after = conn.execute(
            "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
            (si_id,),
        ).fetchone()
        assert Decimal(si_after["outstanding_amount"]) == Decimal("0")
        assert si_after["status"] == "paid"

    # -------------------------------------------------------------------
    # 2. Credit note creation against submitted invoice
    # -------------------------------------------------------------------

    def test_credit_note_creation(self, fresh_db):
        """Create a credit note against a submitted sales invoice.
        Verify the credit note record has correct fields."""
        conn = fresh_db
        env = setup_phase2_environment(conn)

        si_id, grand_total = _create_and_submit_sales_invoice(conn, env,
                                                               qty="8", rate="50.00")
        # grand_total = 8 * 50 = 400.00

        # Create credit note for partial: 3 items at 50
        cn_items = json.dumps([{
            "item_id": env["item_id"],
            "qty": "3",
            "rate": "50.00",
        }])
        r = _call_action("erpclaw-selling", "create-credit-note", conn,
                          against_invoice_id=si_id,
                          items=cn_items,
                          posting_date="2026-06-10",
                          reason="Partial return")
        assert r["status"] == "ok"
        cn_id = r["credit_note_id"]
        assert r["against_invoice_id"] == si_id
        assert r["is_return"] is True

        # Verify the credit note in DB
        cn = conn.execute("SELECT * FROM sales_invoice WHERE id = ?",
                          (cn_id,)).fetchone()
        assert cn is not None
        assert cn["is_return"] == 1
        assert cn["return_against"] == si_id
        assert cn["status"] == "draft"
        # grand_total should be negative (credit note)
        assert Decimal(cn["grand_total"]) == Decimal("-150.00")

        # Verify credit note items
        cn_items_rows = conn.execute(
            "SELECT * FROM sales_invoice_item WHERE sales_invoice_id = ?",
            (cn_id,),
        ).fetchall()
        assert len(cn_items_rows) == 1
        # Quantity should be negative for returns
        assert Decimal(cn_items_rows[0]["quantity"]) == Decimal("-3")

    # -------------------------------------------------------------------
    # 3. Credit note GL reversal
    # -------------------------------------------------------------------

    def test_credit_note_gl_reversal(self, fresh_db):
        """Submit a credit note and verify it creates reversal GL entries:
        CR Receivable (opposite of original DR) and DR Revenue (opposite
        of original CR)."""
        conn = fresh_db
        env = setup_phase2_environment(conn)

        si_id, _ = _create_and_submit_sales_invoice(conn, env,
                                                     qty="10", rate="100.00")
        # grand_total = 1000.00

        # Count original GL entries
        orig_gl = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'sales_invoice' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (si_id,),
        ).fetchall()
        assert len(orig_gl) >= 2

        # Create and submit full credit note
        cn_items = json.dumps([{
            "item_id": env["item_id"],
            "qty": "10",
            "rate": "100.00",
        }])
        r = _call_action("erpclaw-selling", "create-credit-note", conn,
                          against_invoice_id=si_id,
                          items=cn_items,
                          posting_date="2026-06-15",
                          reason="Defective batch")
        cn_id = r["credit_note_id"]

        r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                          sales_invoice_id=cn_id)
        assert r["status"] == "ok"

        # Credit note GL entries
        cn_gl = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'credit_note' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (cn_id,),
        ).fetchall()
        assert len(cn_gl) >= 2

        # Credit note should CR the receivable account (reverse of original DR)
        recv_gl = [g for g in cn_gl if g["account_id"] == env["receivable_id"]]
        assert len(recv_gl) >= 1, "Credit note should have GL entry for receivable"
        recv_credit = sum(Decimal(g["credit"]) for g in recv_gl)
        assert recv_credit == Decimal("1000.00"), (
            f"Expected 1000.00 credit to receivable, got {recv_credit}"
        )

        # Credit note should DR the income account (reverse of original CR)
        income_gl = [g for g in cn_gl if g["account_id"] == env["income_id"]]
        assert len(income_gl) >= 1, "Credit note should have GL entry for income"
        income_debit = sum(Decimal(g["debit"]) for g in income_gl)
        assert income_debit == Decimal("1000.00"), (
            f"Expected 1000.00 debit to income, got {income_debit}"
        )

        # Credit note GL must be balanced
        cn_total_debit = sum(Decimal(g["debit"]) for g in cn_gl)
        cn_total_credit = sum(Decimal(g["credit"]) for g in cn_gl)
        assert abs(cn_total_debit - cn_total_credit) < Decimal("0.01")

    # -------------------------------------------------------------------
    # 4. Credit note reduces customer outstanding
    # -------------------------------------------------------------------

    def test_credit_note_outstanding(self, fresh_db):
        """After submitting a credit note and updating outstanding,
        the customer's invoice outstanding should be reduced."""
        conn = fresh_db
        env = setup_phase2_environment(conn)

        si_id, _ = _create_and_submit_sales_invoice(conn, env,
                                                     qty="10", rate="80.00")
        # grand_total = 800.00

        # Verify initial outstanding
        si = conn.execute("SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
                          (si_id,)).fetchone()
        assert Decimal(si["outstanding_amount"]) == Decimal("800.00")

        # Create and submit partial credit note: 4 items @ 80 = 320
        cn_items = json.dumps([{
            "item_id": env["item_id"],
            "qty": "4",
            "rate": "80.00",
        }])
        r = _call_action("erpclaw-selling", "create-credit-note", conn,
                          against_invoice_id=si_id,
                          items=cn_items,
                          posting_date="2026-06-10",
                          reason="Quality issue")
        cn_id = r["credit_note_id"]

        r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                          sales_invoice_id=cn_id)
        assert r["status"] == "ok"

        # Update outstanding by credit note amount (320.00)
        r = _call_action("erpclaw-selling", "update-invoice-outstanding", conn,
                          sales_invoice_id=si_id,
                          amount="320.00")
        assert r["status"] == "ok"

        # Verify outstanding reduced
        si_after = conn.execute(
            "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
            (si_id,),
        ).fetchone()
        assert Decimal(si_after["outstanding_amount"]) == Decimal("480.00")
        assert si_after["status"] == "partially_paid"

    # -------------------------------------------------------------------
    # 5. Partial credit note
    # -------------------------------------------------------------------

    def test_partial_credit_note(self, fresh_db):
        """Create a credit note for fewer items than the original invoice.
        Verify partial amounts in credit note record and GL."""
        conn = fresh_db
        env = setup_phase2_environment(conn)

        si_id, _ = _create_and_submit_sales_invoice(conn, env,
                                                     qty="20", rate="25.00")
        # grand_total = 500.00

        # Credit note for 5 items at 25.00 = 125.00
        cn_items = json.dumps([{
            "item_id": env["item_id"],
            "qty": "5",
            "rate": "25.00",
        }])
        r = _call_action("erpclaw-selling", "create-credit-note", conn,
                          against_invoice_id=si_id,
                          items=cn_items,
                          posting_date="2026-06-12",
                          reason="Partial defect")
        assert r["status"] == "ok"
        cn_id = r["credit_note_id"]

        # Verify credit note total is -125.00
        cn = conn.execute("SELECT * FROM sales_invoice WHERE id = ?",
                          (cn_id,)).fetchone()
        assert Decimal(cn["grand_total"]) == Decimal("-125.00")
        assert Decimal(cn["total_amount"]) == Decimal("-125.00")

        # Submit and verify GL
        r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                          sales_invoice_id=cn_id)
        assert r["status"] == "ok"

        cn_gl = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'credit_note' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (cn_id,),
        ).fetchall()
        assert len(cn_gl) >= 2

        # Verify the GL amounts are 125.00 (absolute)
        cn_debit = sum(Decimal(g["debit"]) for g in cn_gl)
        cn_credit = sum(Decimal(g["credit"]) for g in cn_gl)
        assert cn_debit == Decimal("125.00"), f"Expected 125 debit, got {cn_debit}"
        assert cn_credit == Decimal("125.00"), f"Expected 125 credit, got {cn_credit}"

    # -------------------------------------------------------------------
    # 6. Debit note creation against purchase invoice
    # -------------------------------------------------------------------

    def test_debit_note_creation(self, fresh_db):
        """Create a debit note against a submitted purchase invoice.
        Verify the debit note record has correct fields."""
        conn = fresh_db
        env = setup_phase2_environment(conn)
        _set_default_expense_account(conn, env["company_id"], env["expense_id"])

        pi_id, grand_total = _create_and_submit_purchase_invoice(conn, env,
                                                                  qty="10", rate="50.00")
        # grand_total = 500.00

        # Create debit note for 4 items at 50 = 200
        dn_items = json.dumps([{
            "item_id": env["item_id"],
            "qty": "4",
            "rate": "50.00",
        }])
        r = _call_action("erpclaw-buying", "create-debit-note", conn,
                          against_invoice_id=pi_id,
                          items=dn_items,
                          posting_date="2026-06-15",
                          reason="Defective goods")
        assert r["status"] == "ok"
        dn_id = r["debit_note_id"]
        assert r["against_invoice_id"] == pi_id

        # Verify debit note total is negative
        assert Decimal(r["total_amount"]) == Decimal("-200.00")

        # Verify the debit note record in DB
        dn = conn.execute("SELECT * FROM purchase_invoice WHERE id = ?",
                          (dn_id,)).fetchone()
        assert dn is not None
        assert dn["is_return"] == 1
        assert dn["return_against"] == pi_id
        assert dn["status"] == "draft"
        assert Decimal(dn["grand_total"]) == Decimal("-200.00")

        # Verify debit note items have negative quantities
        dn_items_rows = conn.execute(
            "SELECT * FROM purchase_invoice_item WHERE purchase_invoice_id = ?",
            (dn_id,),
        ).fetchall()
        assert len(dn_items_rows) == 1
        assert Decimal(dn_items_rows[0]["quantity"]) == Decimal("-4")

    # -------------------------------------------------------------------
    # 7. Debit note GL reversal
    # -------------------------------------------------------------------

    def test_debit_note_gl_reversal(self, fresh_db):
        """Submit a debit note and verify it creates reversal GL entries:
        DR Payable (opposite of original CR) and CR Expense (opposite of
        original DR)."""
        conn = fresh_db
        env = setup_phase2_environment(conn)
        _set_default_expense_account(conn, env["company_id"], env["expense_id"])

        pi_id, _ = _create_and_submit_purchase_invoice(conn, env,
                                                        qty="10", rate="50.00")
        # grand_total = 500.00

        # Original GL: DR Expense 500, CR Payable 500
        orig_gl = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'purchase_invoice' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (pi_id,),
        ).fetchall()
        orig_debit = sum(Decimal(g["debit"]) for g in orig_gl)
        orig_credit = sum(Decimal(g["credit"]) for g in orig_gl)
        assert orig_debit == orig_credit == Decimal("500.00")

        # Create debit note for full return
        dn_items = json.dumps([{
            "item_id": env["item_id"],
            "qty": "10",
            "rate": "50.00",
        }])
        r = _call_action("erpclaw-buying", "create-debit-note", conn,
                          against_invoice_id=pi_id,
                          items=dn_items,
                          posting_date="2026-06-20",
                          reason="Full return")
        dn_id = r["debit_note_id"]

        # Submit debit note (uses submit-purchase-invoice)
        r = _call_action("erpclaw-buying", "submit-purchase-invoice", conn,
                          purchase_invoice_id=dn_id)
        assert r["status"] == "ok"

        # Debit note GL entries
        dn_gl = conn.execute(
            """SELECT * FROM gl_entry
               WHERE voucher_type = 'debit_note' AND voucher_id = ?
                 AND is_cancelled = 0""",
            (dn_id,),
        ).fetchall()
        assert len(dn_gl) >= 2, (
            f"Expected >= 2 GL entries for debit note, got {len(dn_gl)}"
        )

        # Debit note should DR the payable account (reverse of original CR)
        payable_gl = [g for g in dn_gl if g["account_id"] == env["payable_id"]]
        assert len(payable_gl) >= 1, "Debit note should have GL entry for payable"
        payable_debit = sum(Decimal(g["debit"]) for g in payable_gl)
        assert payable_debit == Decimal("500.00"), (
            f"Expected 500.00 debit to payable, got {payable_debit}"
        )

        # Debit note GL must be balanced
        dn_total_debit = sum(Decimal(g["debit"]) for g in dn_gl)
        dn_total_credit = sum(Decimal(g["credit"]) for g in dn_gl)
        assert abs(dn_total_debit - dn_total_credit) < Decimal("0.01"), (
            f"Debit note GL not balanced: D={dn_total_debit}, C={dn_total_credit}"
        )

    # -------------------------------------------------------------------
    # 8. Debit note reduces supplier outstanding
    # -------------------------------------------------------------------

    def test_debit_note_outstanding(self, fresh_db):
        """After creating a debit note and updating outstanding,
        the supplier's invoice outstanding should be reduced."""
        conn = fresh_db
        env = setup_phase2_environment(conn)
        _set_default_expense_account(conn, env["company_id"], env["expense_id"])

        pi_id, _ = _create_and_submit_purchase_invoice(conn, env,
                                                        qty="10", rate="50.00")
        # grand_total = 500.00

        # Verify initial outstanding
        pi = conn.execute("SELECT outstanding_amount FROM purchase_invoice WHERE id = ?",
                          (pi_id,)).fetchone()
        assert Decimal(pi["outstanding_amount"]) == Decimal("500.00")

        # Create debit note for partial: 3 items @ 50 = 150
        dn_items = json.dumps([{
            "item_id": env["item_id"],
            "qty": "3",
            "rate": "50.00",
        }])
        r = _call_action("erpclaw-buying", "create-debit-note", conn,
                          against_invoice_id=pi_id,
                          items=dn_items,
                          posting_date="2026-06-15",
                          reason="Partial return")
        dn_id = r["debit_note_id"]

        # Update outstanding by debit note amount
        r = _call_action("erpclaw-buying", "update-invoice-outstanding", conn,
                          purchase_invoice_id=pi_id,
                          amount="150.00")
        assert r["status"] == "ok"
        assert Decimal(r["outstanding_amount"]) == Decimal("350.00")

        # Verify in DB
        pi_after = conn.execute(
            "SELECT outstanding_amount, status FROM purchase_invoice WHERE id = ?",
            (pi_id,),
        ).fetchone()
        assert Decimal(pi_after["outstanding_amount"]) == Decimal("350.00")
        assert pi_after["status"] == "partially_paid"

    # -------------------------------------------------------------------
    # 9. Trial balance balanced after credit note
    # -------------------------------------------------------------------

    def test_credit_note_trial_balance(self, fresh_db):
        """After issuing a sales invoice and a credit note, the trial
        balance should remain balanced."""
        conn = fresh_db
        env = setup_phase2_environment(conn)

        si_id, _ = _create_and_submit_sales_invoice(conn, env,
                                                     qty="10", rate="100.00")
        # grand_total = 1000.00

        # Create and submit credit note for 6 items @ 100 = 600
        cn_items = json.dumps([{
            "item_id": env["item_id"],
            "qty": "6",
            "rate": "100.00",
        }])
        r = _call_action("erpclaw-selling", "create-credit-note", conn,
                          against_invoice_id=si_id,
                          items=cn_items,
                          posting_date="2026-06-15",
                          reason="Partial return")
        cn_id = r["credit_note_id"]

        r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                          sales_invoice_id=cn_id)
        assert r["status"] == "ok"

        # Run trial balance
        r = _call_action("erpclaw-reports", "trial-balance", conn,
                          company_id=env["company_id"],
                          from_date="2026-01-01",
                          to_date="2026-12-31")
        assert r["status"] == "ok"

        total_debit = Decimal(r["total_debit"])
        total_credit = Decimal(r["total_credit"])
        assert abs(total_debit - total_credit) < Decimal("0.01"), (
            f"Trial balance not balanced after credit note: "
            f"debit={total_debit}, credit={total_credit}"
        )

        # Also verify via check-gl-integrity
        r = _call_action("erpclaw-gl", "check-gl-integrity", conn,
                          company_id=env["company_id"])
        assert r["status"] == "ok"
        assert r["balanced"] is True

    # -------------------------------------------------------------------
    # 10. Full return cycle: SI -> credit note -> payment reconciliation
    # -------------------------------------------------------------------

    def test_full_return_cycle(self, fresh_db):
        """Full return lifecycle: create sales invoice, submit it,
        receive partial payment, create credit note for remaining,
        and verify all outstanding is settled and GL is balanced."""
        conn = fresh_db
        env = setup_phase2_environment(conn)

        si_id, grand_total = _create_and_submit_sales_invoice(conn, env,
                                                               qty="10", rate="100.00")
        # grand_total = 1000.00
        assert grand_total == "1000.00"

        # Step 1: Receive partial payment of 600.00
        r = _call_action("erpclaw-payments", "add-payment", conn,
                          company_id=env["company_id"],
                          payment_type="receive",
                          posting_date="2026-06-10",
                          party_type="customer",
                          party_id=env["customer_id"],
                          paid_from_account=env["receivable_id"],
                          paid_to_account=env["bank_id"],
                          paid_amount="600.00")
        assert r["status"] == "ok"
        pe_id = r["payment_entry_id"]

        r = _call_action("erpclaw-payments", "submit-payment", conn,
                          payment_entry_id=pe_id)
        assert r["status"] == "ok"

        # Update invoice outstanding with partial payment
        _call_action("erpclaw-selling", "update-invoice-outstanding", conn,
                      sales_invoice_id=si_id,
                      amount="600.00")

        # Verify outstanding = 400.00
        si = conn.execute("SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
                          (si_id,)).fetchone()
        assert Decimal(si["outstanding_amount"]) == Decimal("400.00")

        # Step 2: Create credit note for remaining 400.00 (4 items @ 100)
        cn_items = json.dumps([{
            "item_id": env["item_id"],
            "qty": "4",
            "rate": "100.00",
        }])
        r = _call_action("erpclaw-selling", "create-credit-note", conn,
                          against_invoice_id=si_id,
                          items=cn_items,
                          posting_date="2026-06-15",
                          reason="Returned remaining items")
        cn_id = r["credit_note_id"]

        r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                          sales_invoice_id=cn_id)
        assert r["status"] == "ok"

        # Step 3: Update outstanding with credit note amount
        _call_action("erpclaw-selling", "update-invoice-outstanding", conn,
                      sales_invoice_id=si_id,
                      amount="400.00")

        # Verify outstanding is now 0
        si_final = conn.execute(
            "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
            (si_id,),
        ).fetchone()
        assert Decimal(si_final["outstanding_amount"]) == Decimal("0")
        assert si_final["status"] == "paid"

        # Step 4: Verify overall GL integrity
        r = _call_action("erpclaw-gl", "check-gl-integrity", conn,
                          company_id=env["company_id"])
        assert r["status"] == "ok"
        assert r["balanced"] is True, f"GL not balanced: {r.get('difference')}"

        # Step 5: Verify trial balance
        r = _call_action("erpclaw-reports", "trial-balance", conn,
                          company_id=env["company_id"],
                          from_date="2026-01-01",
                          to_date="2026-12-31")
        assert r["status"] == "ok"
        total_debit = Decimal(r["total_debit"])
        total_credit = Decimal(r["total_credit"])
        assert abs(total_debit - total_credit) < Decimal("0.01"), (
            f"Trial balance not balanced: D={total_debit}, C={total_credit}"
        )
