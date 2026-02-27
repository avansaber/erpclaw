"""Phase 2 cross-skill integration tests: Order-to-Cash workflow (XS-11 through XS-17).

Tests the full selling cycle from quotation through to payment,
verifying GL entries, SLE entries, PLE entries, and data consistency
across erpclaw-selling, erpclaw-payments, and erpclaw-gl skills.
"""
import json
from decimal import Decimal

from helpers import (
    _call_action,
    setup_phase2_environment,
    seed_stock_for_item,
    create_test_item,
    create_test_account,
)


# ---------------------------------------------------------------------------
# Helper: build items JSON for a single item
# ---------------------------------------------------------------------------

def _items_json(env, qty="10", rate="50.00"):
    """Build the standard items JSON payload."""
    return json.dumps([{
        "item_id": env["item_id"],
        "qty": qty,
        "rate": rate,
        "warehouse_id": env["warehouse_id"],
    }])


# ---------------------------------------------------------------------------
# Helper: run the full quotation-to-invoice pipeline, returning all IDs
# ---------------------------------------------------------------------------

def _run_full_pipeline(conn, env, items=None, tax_template_id=None):
    """Run quot -> submit quot -> convert to SO -> submit SO -> DN -> submit DN
    -> invoice -> submit invoice.  Returns dict of all created IDs + results."""
    items_j = items or _items_json(env)

    # 1. add-quotation
    r = _call_action("erpclaw-selling", "add-quotation", conn,
                     customer_id=env["customer_id"],
                     posting_date="2026-06-15",
                     items=items_j,
                     company_id=env["company_id"],
                     tax_template_id=tax_template_id)
    assert r["status"] == "ok", f"add-quotation failed: {r}"
    q_id = r["quotation_id"]

    # 2. submit-quotation
    r = _call_action("erpclaw-selling", "submit-quotation", conn,
                     quotation_id=q_id)
    assert r["status"] == "ok", f"submit-quotation failed: {r}"

    # 3. convert-quotation-to-so
    r = _call_action("erpclaw-selling", "convert-quotation-to-so", conn,
                     quotation_id=q_id,
                     delivery_date="2026-07-01")
    assert r["status"] == "ok", f"convert-quotation-to-so failed: {r}"
    so_id = r["sales_order_id"]

    # Quotation items don't carry warehouse_id, so set it on SO items
    # by parsing the original items JSON to get the warehouse mapping.
    parsed_items = json.loads(items_j)
    for pi in parsed_items:
        if pi.get("warehouse_id"):
            conn.execute(
                "UPDATE sales_order_item SET warehouse_id = ? WHERE sales_order_id = ? AND item_id = ?",
                (pi["warehouse_id"], so_id, pi["item_id"]),
            )
    conn.commit()

    # 4. submit-sales-order
    r = _call_action("erpclaw-selling", "submit-sales-order", conn,
                     sales_order_id=so_id)
    assert r["status"] == "ok", f"submit-sales-order failed: {r}"

    # 5. create-delivery-note
    r = _call_action("erpclaw-selling", "create-delivery-note", conn,
                     sales_order_id=so_id,
                     posting_date="2026-07-01")
    assert r["status"] == "ok", f"create-delivery-note failed: {r}"
    dn_id = r["delivery_note_id"]

    # 6. submit-delivery-note
    r_dn = _call_action("erpclaw-selling", "submit-delivery-note", conn,
                        delivery_note_id=dn_id)
    assert r_dn["status"] == "ok", f"submit-delivery-note failed: {r_dn}"

    # 7. create-sales-invoice (from SO; stock already moved by DN)
    r = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                     sales_order_id=so_id,
                     posting_date="2026-07-02",
                     tax_template_id=tax_template_id)
    assert r["status"] == "ok", f"create-sales-invoice failed: {r}"
    si_id = r["sales_invoice_id"]

    # 8. submit-sales-invoice
    r_si = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                        sales_invoice_id=si_id)
    assert r_si["status"] == "ok", f"submit-sales-invoice failed: {r_si}"

    return {
        "quotation_id": q_id,
        "sales_order_id": so_id,
        "delivery_note_id": dn_id,
        "sales_invoice_id": si_id,
        "dn_result": r_dn,
        "si_result": r_si,
    }


# ---------------------------------------------------------------------------
# XS-11: Full order-to-cash cycle
# ---------------------------------------------------------------------------

def test_XS11_full_order_to_cash(fresh_db):
    """Complete cycle: quotation -> SO -> DN -> invoice.
    Verify customer exists, SO fulfilled, DN submitted, invoice submitted,
    GL balanced, PLE created."""
    conn = fresh_db
    env = setup_phase2_environment(conn)

    ids = _run_full_pipeline(conn, env)

    # 1. Customer exists
    cust = conn.execute("SELECT * FROM customer WHERE id = ?",
                        (env["customer_id"],)).fetchone()
    assert cust is not None
    assert cust["status"] == "active"

    # 2. SO is fully delivered and fully invoiced
    so = conn.execute("SELECT * FROM sales_order WHERE id = ?",
                      (ids["sales_order_id"],)).fetchone()
    assert so is not None
    assert so["status"] in ("fully_delivered", "fully_invoiced")

    # 3. DN is submitted
    dn = conn.execute("SELECT * FROM delivery_note WHERE id = ?",
                      (ids["delivery_note_id"],)).fetchone()
    assert dn is not None
    assert dn["status"] == "submitted"

    # 4. Invoice is submitted
    si = conn.execute("SELECT * FROM sales_invoice WHERE id = ?",
                      (ids["sales_invoice_id"],)).fetchone()
    assert si is not None
    assert si["status"] == "submitted"

    # 5. GL is balanced
    gl_rows = conn.execute(
        "SELECT * FROM gl_entry WHERE is_cancelled = 0"
    ).fetchall()
    assert len(gl_rows) > 0
    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert abs(total_debit - total_credit) < Decimal("0.01"), (
        f"GL not balanced: debit={total_debit}, credit={total_credit}"
    )

    # 6. PLE created for the invoice
    ple = conn.execute(
        "SELECT * FROM payment_ledger_entry WHERE voucher_type = 'sales_invoice' AND voucher_id = ?",
        (ids["sales_invoice_id"],),
    ).fetchall()
    assert len(ple) >= 1, "No PLE entry found for submitted invoice"


# ---------------------------------------------------------------------------
# XS-12: Delivery creates stock and GL
# ---------------------------------------------------------------------------

def test_XS12_delivery_creates_stock_and_gl(fresh_db):
    """Submit delivery note creates SLE entries (negative stock out) and
    GL entries (DR COGS, CR Stock). Verify SLE actual_qty is negative
    and GL is balanced."""
    conn = fresh_db
    env = setup_phase2_environment(conn)

    items_j = _items_json(env, qty="10", rate="50.00")

    # Create SO directly (skip quotation for focused test)
    r = _call_action("erpclaw-selling", "add-sales-order", conn,
                     customer_id=env["customer_id"],
                     posting_date="2026-06-15",
                     items=items_j,
                     company_id=env["company_id"])
    so_id = r["sales_order_id"]

    _call_action("erpclaw-selling", "submit-sales-order", conn,
                 sales_order_id=so_id)

    r = _call_action("erpclaw-selling", "create-delivery-note", conn,
                     sales_order_id=so_id,
                     posting_date="2026-07-01")
    dn_id = r["delivery_note_id"]

    r = _call_action("erpclaw-selling", "submit-delivery-note", conn,
                     delivery_note_id=dn_id)
    assert r["status"] == "ok"
    assert r["sle_entries_created"] > 0
    assert r["gl_entries_created"] > 0

    # Verify SLE: actual_qty should be negative (stock going out)
    sle_rows = conn.execute(
        """SELECT * FROM stock_ledger_entry
           WHERE voucher_type = 'delivery_note' AND voucher_id = ?
             AND is_cancelled = 0""",
        (dn_id,),
    ).fetchall()
    assert len(sle_rows) > 0
    for sle in sle_rows:
        assert Decimal(sle["actual_qty"]) < 0, (
            f"SLE actual_qty should be negative (outgoing), got {sle['actual_qty']}"
        )

    # Verify GL: COGS GL entries are balanced (DR COGS, CR Stock In Hand)
    gl_rows = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'delivery_note' AND voucher_id = ?
             AND is_cancelled = 0""",
        (dn_id,),
    ).fetchall()
    assert len(gl_rows) >= 2, f"Expected at least 2 GL entries, got {len(gl_rows)}"

    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert abs(total_debit - total_credit) < Decimal("0.01"), (
        f"DN GL not balanced: debit={total_debit}, credit={total_credit}"
    )


# ---------------------------------------------------------------------------
# XS-13: Invoice creates GL and PLE
# ---------------------------------------------------------------------------

def test_XS13_invoice_creates_gl_and_ple(fresh_db):
    """Submit sales invoice creates GL entries (DR Receivable, CR Revenue)
    and PLE entry. Verify GL balanced, PLE amount matches invoice grand_total."""
    conn = fresh_db
    env = setup_phase2_environment(conn)

    items_j = _items_json(env, qty="10", rate="50.00")

    # Create SO -> DN -> submit DN -> create invoice -> submit invoice
    r = _call_action("erpclaw-selling", "add-sales-order", conn,
                     customer_id=env["customer_id"],
                     posting_date="2026-06-15",
                     items=items_j,
                     company_id=env["company_id"])
    so_id = r["sales_order_id"]

    _call_action("erpclaw-selling", "submit-sales-order", conn,
                 sales_order_id=so_id)

    r = _call_action("erpclaw-selling", "create-delivery-note", conn,
                     sales_order_id=so_id, posting_date="2026-07-01")
    dn_id = r["delivery_note_id"]

    _call_action("erpclaw-selling", "submit-delivery-note", conn,
                 delivery_note_id=dn_id)

    r = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                     sales_order_id=so_id, posting_date="2026-07-02")
    si_id = r["sales_invoice_id"]
    assert r["status"] == "ok"

    r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                     sales_invoice_id=si_id)
    assert r["status"] == "ok"
    assert r["gl_entries_created"] >= 2

    # Get the invoice grand_total from DB
    si = conn.execute("SELECT * FROM sales_invoice WHERE id = ?",
                      (si_id,)).fetchone()
    grand_total = Decimal(si["grand_total"])

    # Verify GL entries: DR Receivable, CR Revenue
    gl_rows = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'sales_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (si_id,),
    ).fetchall()
    assert len(gl_rows) >= 2

    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert abs(total_debit - total_credit) < Decimal("0.01"), (
        f"Invoice GL not balanced: debit={total_debit}, credit={total_credit}"
    )

    # Check there is a debit to receivable = grand_total
    recv_entries = [g for g in gl_rows
                    if g["account_id"] == env["receivable_id"]
                    and Decimal(g["debit"]) > 0]
    assert len(recv_entries) >= 1, "No debit to receivable account found"
    recv_debit = sum(Decimal(g["debit"]) for g in recv_entries)
    assert recv_debit == grand_total, (
        f"Receivable debit {recv_debit} != grand_total {grand_total}"
    )

    # Verify PLE
    ple = conn.execute(
        """SELECT * FROM payment_ledger_entry
           WHERE voucher_type = 'sales_invoice' AND voucher_id = ?""",
        (si_id,),
    ).fetchall()
    assert len(ple) >= 1, "No PLE for submitted invoice"
    ple_amount = Decimal(ple[0]["amount"])
    assert ple_amount == grand_total, (
        f"PLE amount {ple_amount} != grand_total {grand_total}"
    )


# ---------------------------------------------------------------------------
# XS-14: Invoice with tax creates tax GL
# ---------------------------------------------------------------------------

def test_XS14_invoice_with_tax_creates_tax_gl(fresh_db):
    """Submit sales invoice with tax_template_id creates additional GL entries
    for tax (CR tax account). Verify net_total + tax = grand_total, tax GL
    amount correct."""
    conn = fresh_db
    env = setup_phase2_environment(conn)

    items_j = _items_json(env, qty="10", rate="50.00")  # net = 500.00

    # Create SO with tax template
    r = _call_action("erpclaw-selling", "add-sales-order", conn,
                     customer_id=env["customer_id"],
                     posting_date="2026-06-15",
                     items=items_j,
                     company_id=env["company_id"],
                     tax_template_id=env["sales_tax_id"])
    so_id = r["sales_order_id"]

    _call_action("erpclaw-selling", "submit-sales-order", conn,
                 sales_order_id=so_id)

    r = _call_action("erpclaw-selling", "create-delivery-note", conn,
                     sales_order_id=so_id, posting_date="2026-07-01")
    dn_id = r["delivery_note_id"]

    _call_action("erpclaw-selling", "submit-delivery-note", conn,
                 delivery_note_id=dn_id)

    r = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                     sales_order_id=so_id, posting_date="2026-07-02",
                     tax_template_id=env["sales_tax_id"])
    si_id = r["sales_invoice_id"]
    assert r["tax_amount"] != "0", f"Expected non-zero tax, got {r['tax_amount']}"

    r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                     sales_invoice_id=si_id)
    assert r["status"] == "ok"

    # Get invoice details from DB
    si = conn.execute("SELECT * FROM sales_invoice WHERE id = ?",
                      (si_id,)).fetchone()
    net_total = Decimal(si["total_amount"])
    tax_amount = Decimal(si["tax_amount"])
    grand_total = Decimal(si["grand_total"])

    # Verify net + tax = grand_total
    assert abs((net_total + tax_amount) - grand_total) < Decimal("0.01"), (
        f"net {net_total} + tax {tax_amount} != grand_total {grand_total}"
    )

    # Expected: net = 500.00, tax = 8% of 500 = 40.00, grand_total = 540.00
    assert net_total == Decimal("500.00"), f"Expected net 500.00, got {net_total}"
    assert tax_amount == Decimal("40.00"), f"Expected tax 40.00, got {tax_amount}"
    assert grand_total == Decimal("540.00"), f"Expected grand 540.00, got {grand_total}"

    # Verify GL has tax account entry
    gl_rows = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'sales_invoice' AND voucher_id = ?
             AND is_cancelled = 0""",
        (si_id,),
    ).fetchall()

    tax_gl = [g for g in gl_rows if g["account_id"] == env["sales_tax_acct"]]
    assert len(tax_gl) >= 1, "No GL entry for sales tax account"

    tax_gl_credit = sum(Decimal(g["credit"]) for g in tax_gl)
    assert tax_gl_credit == Decimal("40.00"), (
        f"Tax GL credit {tax_gl_credit} != expected 40.00"
    )

    # Overall GL must be balanced
    total_debit = sum(Decimal(r["debit"]) for r in gl_rows)
    total_credit = sum(Decimal(r["credit"]) for r in gl_rows)
    assert abs(total_debit - total_credit) < Decimal("0.01"), (
        f"Invoice GL not balanced: debit={total_debit}, credit={total_credit}"
    )


# ---------------------------------------------------------------------------
# XS-15: Credit note reverses GL
# ---------------------------------------------------------------------------

def test_XS15_credit_note_reverses_gl(fresh_db):
    """Submit invoice, then create-credit-note against it. Verify credit note
    creates reversal GL entries (CR Receivable, DR Revenue), and original
    invoice outstanding is reduced."""
    conn = fresh_db
    env = setup_phase2_environment(conn)

    items_j = _items_json(env, qty="5", rate="100.00")  # 500.00

    # Create and submit a standalone invoice
    r = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                     customer_id=env["customer_id"],
                     posting_date="2026-07-02",
                     items=items_j,
                     company_id=env["company_id"])
    si_id = r["sales_invoice_id"]
    assert r["grand_total"] == "500.00"

    r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                     sales_invoice_id=si_id)
    assert r["status"] == "ok"

    # Get original outstanding
    si_before = conn.execute(
        "SELECT outstanding_amount FROM sales_invoice WHERE id = ?",
        (si_id,),
    ).fetchone()
    assert Decimal(si_before["outstanding_amount"]) == Decimal("500.00")

    # Create credit note for full amount
    cn_items = json.dumps([{
        "item_id": env["item_id"],
        "qty": "5",
        "rate": "100.00",
    }])
    r = _call_action("erpclaw-selling", "create-credit-note", conn,
                     against_invoice_id=si_id,
                     items=cn_items,
                     posting_date="2026-07-05",
                     reason="Defective goods")
    assert r["status"] == "ok"
    cn_id = r["credit_note_id"]

    # Submit credit note
    r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                     sales_invoice_id=cn_id)
    assert r["status"] == "ok"

    # Verify credit note GL entries exist and are balanced.
    # Credit notes use voucher_type='credit_note' and post with abs() amounts
    # on swapped DR/CR sides (CR Receivable, DR Revenue).
    cn_gl = conn.execute(
        """SELECT * FROM gl_entry
           WHERE voucher_type = 'credit_note' AND voucher_id = ?
             AND is_cancelled = 0""",
        (cn_id,),
    ).fetchall()
    assert len(cn_gl) >= 2, f"Expected at least 2 GL entries for credit note, got {len(cn_gl)}"

    # Credit note should touch receivable account
    recv_gl = [g for g in cn_gl if g["account_id"] == env["receivable_id"]]
    assert len(recv_gl) >= 1, "Credit note should have GL entry for receivable account"

    # Credit note should touch income account
    income_gl = [g for g in cn_gl if g["account_id"] == env["income_id"]]
    assert len(income_gl) >= 1, "Credit note should have GL entry for income account"

    # Credit note GL must be balanced
    cn_debit = sum(Decimal(r["debit"]) for r in cn_gl)
    cn_credit = sum(Decimal(r["credit"]) for r in cn_gl)
    assert abs(cn_debit - cn_credit) < Decimal("0.01"), (
        f"Credit note GL not balanced: debit={cn_debit}, credit={cn_credit}"
    )

    # Credit note PLE should exist and have the same magnitude as original
    cn_ple = conn.execute(
        """SELECT * FROM payment_ledger_entry
           WHERE voucher_type = 'credit_note' AND voucher_id = ?""",
        (cn_id,),
    ).fetchall()
    assert len(cn_ple) >= 1, "Credit note should create PLE entry"

    # Update original invoice outstanding using credit note amount
    _call_action("erpclaw-selling", "update-invoice-outstanding", conn,
                 sales_invoice_id=si_id,
                 amount="500.00")

    # Verify original invoice outstanding is reduced
    si_after = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (si_id,),
    ).fetchone()
    assert Decimal(si_after["outstanding_amount"]) == Decimal("0"), (
        f"Outstanding should be 0, got {si_after['outstanding_amount']}"
    )
    assert si_after["status"] == "paid"


# ---------------------------------------------------------------------------
# XS-16: Payment closes invoice
# ---------------------------------------------------------------------------

def test_XS16_payment_closes_invoice(fresh_db):
    """Submit invoice, then create+submit payment that reduces outstanding
    to 0. Verify invoice outstanding becomes '0', PLE entries net to 0."""
    conn = fresh_db
    env = setup_phase2_environment(conn)

    items_j = _items_json(env, qty="10", rate="50.00")  # 500.00

    # Create and submit invoice
    r = _call_action("erpclaw-selling", "create-sales-invoice", conn,
                     customer_id=env["customer_id"],
                     posting_date="2026-07-02",
                     items=items_j,
                     company_id=env["company_id"])
    si_id = r["sales_invoice_id"]
    grand_total = r["grand_total"]
    assert grand_total == "500.00"

    r = _call_action("erpclaw-selling", "submit-sales-invoice", conn,
                     sales_invoice_id=si_id)
    assert r["status"] == "ok"

    # Create payment to receive the full invoice amount
    r = _call_action("erpclaw-payments", "add-payment", conn,
                     company_id=env["company_id"],
                     payment_type="receive",
                     posting_date="2026-07-10",
                     party_type="customer",
                     party_id=env["customer_id"],
                     paid_from_account=env["receivable_id"],
                     paid_to_account=env["bank_id"],
                     paid_amount=grand_total)
    assert r["status"] == "ok"
    pe_id = r["payment_entry_id"]

    # Submit payment
    r = _call_action("erpclaw-payments", "submit-payment", conn,
                     payment_entry_id=pe_id)
    assert r["status"] == "ok"
    assert r["gl_entries_created"] == 2

    # Update the invoice outstanding via cross-skill call
    _call_action("erpclaw-selling", "update-invoice-outstanding", conn,
                 sales_invoice_id=si_id,
                 amount=grand_total)

    # Verify invoice outstanding is 0
    si = conn.execute(
        "SELECT outstanding_amount, status FROM sales_invoice WHERE id = ?",
        (si_id,),
    ).fetchone()
    assert Decimal(si["outstanding_amount"]) == Decimal("0"), (
        f"Outstanding should be 0, got {si['outstanding_amount']}"
    )
    assert si["status"] == "paid"

    # Verify PLE entries net to 0 for this customer
    # Invoice PLE: +500 (receivable created)
    # Payment PLE: -500 (receivable reduced)
    ple_rows = conn.execute(
        """SELECT * FROM payment_ledger_entry
           WHERE party_type = 'customer' AND party_id = ?""",
        (env["customer_id"],),
    ).fetchall()
    assert len(ple_rows) >= 2, f"Expected at least 2 PLE rows, got {len(ple_rows)}"

    ple_net = sum(Decimal(r["amount"]) for r in ple_rows)
    assert abs(ple_net) < Decimal("0.01"), (
        f"PLE should net to 0, got {ple_net}"
    )


# ---------------------------------------------------------------------------
# XS-17: GL balanced after full selling cycle
# ---------------------------------------------------------------------------

def test_XS17_gl_balanced_after_selling_cycle(fresh_db):
    """Run a full selling cycle with multiple items and tax, verify
    check-gl-integrity passes and SUM(debit) = SUM(credit) on all
    non-cancelled GL entries."""
    conn = fresh_db
    env = setup_phase2_environment(conn)

    # Create a second item
    item2_id = create_test_item(conn, item_code="SKU-002", item_name="Gadget B",
                                standard_rate="75.00")
    seed_stock_for_item(conn, item2_id, env["warehouse_id"],
                        qty="100", rate="75.00")

    # Multi-item order WITH tax
    items_j = json.dumps([
        {"item_id": env["item_id"], "qty": "5", "rate": "50.00",
         "warehouse_id": env["warehouse_id"]},
        {"item_id": item2_id, "qty": "3", "rate": "75.00",
         "warehouse_id": env["warehouse_id"]},
    ])
    # net = (5*50) + (3*75) = 250 + 225 = 475
    # tax = 8% of 475 = 38.00
    # grand = 513.00

    # Run full pipeline with tax
    ids = _run_full_pipeline(conn, env, items=items_j,
                             tax_template_id=env["sales_tax_id"])

    # Now also create a payment for the full amount
    si = conn.execute("SELECT grand_total FROM sales_invoice WHERE id = ?",
                      (ids["sales_invoice_id"],)).fetchone()
    grand_total = si["grand_total"]

    r = _call_action("erpclaw-payments", "add-payment", conn,
                     company_id=env["company_id"],
                     payment_type="receive",
                     posting_date="2026-07-15",
                     party_type="customer",
                     party_id=env["customer_id"],
                     paid_from_account=env["receivable_id"],
                     paid_to_account=env["bank_id"],
                     paid_amount=grand_total)
    pe_id = r["payment_entry_id"]

    _call_action("erpclaw-payments", "submit-payment", conn,
                 payment_entry_id=pe_id)

    # 1. check-gl-integrity via erpclaw-gl
    r = _call_action("erpclaw-gl", "check-gl-integrity", conn,
                     company_id=env["company_id"])
    assert r["status"] == "ok"
    assert r["balanced"] is True, (
        f"GL not balanced: difference={r['difference']}"
    )

    # 2. Verify directly: SUM(debit) = SUM(credit) for all non-cancelled GL
    totals = conn.execute(
        """SELECT COALESCE(SUM(CAST(debit AS REAL)), 0) as total_debit,
                  COALESCE(SUM(CAST(credit AS REAL)), 0) as total_credit
           FROM gl_entry WHERE is_cancelled = 0"""
    ).fetchone()
    diff = abs(totals["total_debit"] - totals["total_credit"])
    assert diff < 0.01, (
        f"Global GL not balanced: debit={totals['total_debit']}, "
        f"credit={totals['total_credit']}, diff={diff}"
    )

    # 3. Per-voucher balance check
    vouchers = conn.execute(
        """SELECT voucher_type, voucher_id,
                  SUM(CAST(debit AS REAL)) as vd,
                  SUM(CAST(credit AS REAL)) as vc
           FROM gl_entry WHERE is_cancelled = 0
           GROUP BY voucher_type, voucher_id"""
    ).fetchall()
    for v in vouchers:
        v_diff = abs(v["vd"] - v["vc"])
        assert v_diff < 0.01, (
            f"Voucher {v['voucher_type']}:{v['voucher_id']} not balanced: "
            f"debit={v['vd']}, credit={v['vc']}"
        )
