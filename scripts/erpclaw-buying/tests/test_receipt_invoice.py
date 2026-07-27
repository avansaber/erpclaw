"""Tests for erpclaw-buying purchase receipts and invoices.

Actions tested:
  - create-purchase-receipt, get-purchase-receipt, list-purchase-receipts
  - submit-purchase-receipt, cancel-purchase-receipt
  - create-purchase-invoice, update-purchase-invoice, get-purchase-invoice
  - list-purchase-invoices, submit-purchase-invoice, cancel-purchase-invoice
  - create-debit-note, update-invoice-outstanding
"""
import importlib.util
import json
import os
import pytest
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _load_invariant_engine():
    """Defensive monorepo-harness import (test_inv25_flows.py pattern): the
    published skill tree has no testing/ dir, so engine-backed pins skip."""
    cur = os.path.dirname(os.path.abspath(__file__))
    while True:
        if os.path.exists(os.path.join(cur, "CLAUDE.md")) or \
                os.path.isdir(os.path.join(cur, ".git")):
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent
    path = os.path.join(cur, "testing", "invariant_engine.py")
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location("invariant_engine_pin_buy", path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


_inv_engine = _load_invariant_engine()


def _inv25(conn):
    if _inv_engine is None:
        pytest.skip("invariant_engine harness not present (published skill tree)")
    _inv_engine._ensure_decimal_sum(conn)
    return _inv_engine._check_inv25_ar_summary_detail(conn)


def _inv22(conn):
    if _inv_engine is None:
        pytest.skip("invariant_engine harness not present (published skill tree)")
    _inv_engine._ensure_decimal_sum(conn)
    return _inv_engine._check_inv22_payment_invoice_reconciliation(conn)


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _create_confirmed_po(conn, env, items_str=None):
    """Create and confirm a PO."""
    items_str = items_str or _items(env, ("item1", "10", "50.00"))
    po = call_action(mod.add_purchase_order, conn, ns(
        supplier_id=env["supplier"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items_str,
        tax_template_id=None, name=None,
    ))
    assert is_ok(po), f"PO creation failed: {po}"
    submit = call_action(mod.submit_purchase_order, conn, ns(
        purchase_order_id=po["purchase_order_id"],
    ))
    assert is_ok(submit), f"PO submit failed: {submit}"
    return po["purchase_order_id"]


# ──────────────────────────────────────────────────────────────────────────────
# Purchase Receipts
# ──────────────────────────────────────────────────────────────────────────────

class TestCreatePurchaseReceipt:
    def test_create_from_po(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        result = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        assert is_ok(result)
        assert "purchase_receipt_id" in result

    def test_create_missing_po_fails(self, conn, env):
        result = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=None, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        assert is_error(result)


class TestGetPurchaseReceipt:
    def test_get(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        pr = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        result = call_action(mod.get_purchase_receipt, conn, ns(
            purchase_receipt_id=pr["purchase_receipt_id"],
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "items" in result


class TestListPurchaseReceipts:
    def test_list(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        result = call_action(mod.list_purchase_receipts, conn, ns(
            company_id=env["company_id"], search=None,
            from_date=None, to_date=None, pr_status=None,
            supplier_id=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSubmitPurchaseReceipt:
    def test_submit(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        pr = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        result = call_action(mod.submit_purchase_receipt, conn, ns(
            purchase_receipt_id=pr["purchase_receipt_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM purchase_receipt WHERE id=?",
                           (pr["purchase_receipt_id"],)).fetchone()
        assert row["status"] == "submitted"


class TestGRNValuation:
    """FINDING-010 / ADR-0014: receiving against a PO (GRN) values the stock from
    the PO line rate and posts the perpetual inventory GL — the Path B receipt."""

    def test_grn_values_stock_and_posts_inventory_gl(self, conn, env):
        # PO: 100 x Raw Metal @ $6 = $600 (mirrors mfg-j02-procure-to-pay)
        po_id = _create_confirmed_po(
            conn, env, items_str=_items(env, ("item1", "100", "6.00")))
        pr = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        assert is_ok(pr)
        result = call_action(mod.submit_purchase_receipt, conn, ns(
            purchase_receipt_id=pr["purchase_receipt_id"],
        ))
        assert is_ok(result), f"GRN submit failed: {result}"

        # Exactly ONE SLE for the item/warehouse, valued from the PO rate.
        sle_rows = conn.execute(
            "SELECT actual_qty, valuation_rate, stock_value FROM stock_ledger_entry "
            "WHERE voucher_type='purchase_receipt' AND voucher_id=? AND item_id=? "
            "AND warehouse_id=? AND is_cancelled=0",
            (pr["purchase_receipt_id"], env["item1"], env["warehouse"]),
        ).fetchall()
        assert len(sle_rows) == 1, f"expected exactly one SLE, got {len(sle_rows)}"
        sle = sle_rows[0]
        assert Decimal(sle["actual_qty"]) == Decimal("100")
        assert Decimal(sle["valuation_rate"]) == Decimal("6.00")
        assert Decimal(sle["stock_value"]) == Decimal("600.00")

        # Inventory GL: exactly 2 legs, DR stock 600 / CR SRNB 600, balanced.
        gl_rows = conn.execute(
            "SELECT account_id, debit, credit FROM gl_entry "
            "WHERE voucher_type='purchase_receipt' AND voucher_id=? AND is_cancelled=0",
            (pr["purchase_receipt_id"],),
        ).fetchall()
        assert len(gl_rows) == 2, f"expected 2 GL legs, got {len(gl_rows)}"
        by_acct = {r["account_id"]: r for r in gl_rows}
        assert Decimal(by_acct[env["stock_acct"]]["debit"]) == Decimal("600.00")
        assert Decimal(by_acct[env["srnb"]]["credit"]) == Decimal("600.00")
        total_dr = sum(Decimal(r["debit"]) for r in gl_rows)
        total_cr = sum(Decimal(r["credit"]) for r in gl_rows)
        assert total_dr == total_cr == Decimal("600.00")


class TestCancelPurchaseReceipt:
    def test_cancel(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        pr = call_action(mod.create_purchase_receipt, conn, ns(
            purchase_order_id=po_id, company_id=env["company_id"],
            posting_date="2026-06-20", items=None,
            purchase_receipt_id=None,
        ))
        call_action(mod.submit_purchase_receipt, conn, ns(
            purchase_receipt_id=pr["purchase_receipt_id"],
        ))
        result = call_action(mod.cancel_purchase_receipt, conn, ns(
            purchase_receipt_id=pr["purchase_receipt_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM purchase_receipt WHERE id=?",
                           (pr["purchase_receipt_id"],)).fetchone()
        assert row["status"] == "cancelled"


# ──────────────────────────────────────────────────────────────────────────────
# Purchase Invoices
# ──────────────────────────────────────────────────────────────────────────────

class TestCreatePurchaseInvoice:
    def test_create_from_po(self, conn, env):
        po_id = _create_confirmed_po(conn, env)
        result = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=po_id, purchase_receipt_id=None,
            supplier_id=None, company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=None, tax_template_id=None,
        ))
        assert is_ok(result)
        assert "purchase_invoice_id" in result
        assert Decimal(result["grand_total"]) == Decimal("500.00")

    def test_create_standalone(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        result = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        assert is_ok(result)
        assert Decimal(result["grand_total"]) == Decimal("500.00")

    def test_create_missing_supplier_standalone_fails(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        result = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=None, company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
        ))
        assert is_error(result)


class TestGetPurchaseInvoice:
    def test_get(self, conn, env):
        items = _items(env, ("item1", "3", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
        ))
        result = call_action(mod.get_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "items" in result


class TestListPurchaseInvoices:
    def test_list(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
        ))
        result = call_action(mod.list_purchase_invoices, conn, ns(
            company_id=env["company_id"], search=None,
            from_date=None, to_date=None, pi_status=None,
            supplier_id=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSubmitPurchaseInvoice:
    def test_submit_posts_gl(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        result = call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        assert is_ok(result)

        pi = conn.execute("SELECT status FROM purchase_invoice WHERE id=?",
                          (create["purchase_invoice_id"],)).fetchone()
        assert pi["status"] == "submitted"

        gl_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id=?",
            (create["purchase_invoice_id"],)
        ).fetchone()["cnt"]
        assert gl_count >= 2


class TestCancelPurchaseInvoice:
    def test_cancel(self, conn, env):
        items = _items(env, ("item1", "3", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        result = call_action(mod.cancel_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        assert is_ok(result)

        pi = conn.execute("SELECT status FROM purchase_invoice WHERE id=?",
                          (create["purchase_invoice_id"],)).fetchone()
        assert pi["status"] == "cancelled"


class TestCreateDebitNote:
    def test_debit_note(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        return_items = json.dumps([
            {"item_id": env["item1"], "qty": "2", "rate": "100.00"}
        ])
        result = call_action(mod.create_debit_note, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
            against_invoice_id=create["purchase_invoice_id"],
            reason="Defective goods", posting_date="2026-06-25",
            items=return_items, company_id=env["company_id"],
            due_date=None, tax_template_id=None,
        ))
        assert is_ok(result)
        assert "debit_note_id" in result


class TestUpdateInvoiceOutstanding:
    def test_reduce_outstanding(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        result = call_action(mod.update_invoice_outstanding, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
            amount="200.00",
        ))
        assert is_ok(result)

        pi = conn.execute(
            "SELECT outstanding_amount FROM purchase_invoice WHERE id=?",
            (create["purchase_invoice_id"],)
        ).fetchone()
        assert Decimal(pi["outstanding_amount"]) == Decimal("300.00")

        # QA round-1 DEFECT 1 pin (INV-25 / ADR-0031): the action must post the
        # matching payment-ledger DETAIL row in the same transaction as the
        # SUMMARY write — one invocation without it reds INV-25 permanently.
        assert "payment_ledger_entry_id" in result
        adj = conn.execute(
            "SELECT voucher_type, voucher_id, amount, delinked "
            "FROM payment_ledger_entry WHERE id=?",
            (result["payment_ledger_entry_id"],)
        ).fetchone()
        assert adj is not None
        assert adj["voucher_type"] == "purchase_invoice"
        assert adj["voucher_id"] == create["purchase_invoice_id"]
        assert adj["delinked"] == 0
        assert Decimal(adj["amount"]) == Decimal("-200.00")
        assert _inv25(conn) is None

    def test_full_pay_keeps_inv25_and_inv22_green(self, conn, env):
        """QA round-1 DEFECT 1 pin, full-clear path (AP side): paying a bill to
        zero through update-purchase-outstanding flips it to 'paid' with the
        detail net at exactly zero — INV-25 (always-on) AND INV-22 (paid-scope)
        both green."""
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        r1 = call_action(mod.update_invoice_outstanding, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"], amount="200.00"))
        assert is_ok(r1)
        r2 = call_action(mod.update_invoice_outstanding, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"], amount="300.00"))
        assert is_ok(r2)
        pi = conn.execute(
            "SELECT status, outstanding_amount FROM purchase_invoice WHERE id=?",
            (create["purchase_invoice_id"],)
        ).fetchone()
        assert pi["status"] == "paid"
        assert pi["outstanding_amount"] == "0"
        rows = conn.execute(
            "SELECT amount FROM payment_ledger_entry "
            "WHERE voucher_type='purchase_invoice' AND voucher_id=? AND delinked=0",
            (create["purchase_invoice_id"],)
        ).fetchall()
        assert len(rows) == 3  # submit +500, adjustments -200 and -300
        assert sum(Decimal(r["amount"]) for r in rows) == Decimal("0")
        assert _inv25(conn) is None
        assert _inv22(conn) is None
