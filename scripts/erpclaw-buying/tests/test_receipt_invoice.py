"""Tests for erpclaw-buying purchase receipts and invoices.

Actions tested:
  - create-purchase-receipt, get-purchase-receipt, list-purchase-receipts
  - submit-purchase-receipt, cancel-purchase-receipt
  - create-purchase-invoice, update-purchase-invoice, get-purchase-invoice
  - list-purchase-invoices, submit-purchase-invoice, cancel-purchase-invoice
  - create-debit-note
  - update-invoice-outstanding is RETIRED (TestUpdateInvoiceOutstandingRetired)
"""
import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, init_all_tables,
)
from erpclaw_lib import payment_clearing
from erpclaw_lib.query import P, Q, Table, fn

mod = load_db_query()


_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)
_ROUTER = os.path.join(_SCRIPTS_DIR, "db_query.py")
_IN_TREE_LIB = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")

RETIRED_KEY = "update-invoice-outstanding"
PUBLIC_ACTION = "update-purchase-outstanding"

# The replacement routes. The steer must name ALL of them: the cash flow, the
# reduction flow (payable side: the debit note), and the write-off — a
# retirement without a route is a dead end, and a partial route sends the
# caller somewhere that cannot finish the job they actually had.
SANCTIONED_FLOW = [
    "add-payment",
    "submit-payment",
    "allocate-payment",
    "create-credit-note",
    "create-debit-note",
    "write-off-invoice",
]


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
        pr_t = Table("purchase_receipt")
        pri_t = Table("purchase_receipt_item")
        before = {}
        for name in ("purchase_receipt", "purchase_receipt_item",
                     "audit_log"):
            table = Table(name)
            q = Q.from_(table).select(fn.Count("*").as_("n"))
            before[name] = conn.execute(q.get_sql()).fetchone()["n"]
        result = call_action(mod.get_purchase_receipt, conn, ns(
            purchase_receipt_id=pr["purchase_receipt_id"],
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "items" in result

        # Deepened (m478): get-purchase-receipt is read-only, so the response
        # must repeat the stored receipt — the full PO quantity of 10 x 50.00
        # — exactly, and no table may gain a row.
        pr_id = pr["purchase_receipt_id"]
        q = Q.from_(pr_t).select(pr_t.star).where(pr_t.id == P())
        stored = conn.execute(q.get_sql(), (pr_id,)).fetchone()
        assert (result["document_status"], result["total_qty"],
                result["posting_date"]) == ("draft", "10.00", "2026-06-20")
        assert stored["total_qty"] == "10.00"
        q = (Q.from_(pri_t)
             .select(pri_t.item_id, pri_t.quantity, pri_t.rate, pri_t.amount)
             .where(pri_t.purchase_receipt_id == P()))
        lines = conn.execute(q.get_sql(), (pr_id,)).fetchall()
        assert [(r["item_id"], r["quantity"], r["rate"], r["amount"])
                for r in lines] == [
            (env["item1"], "10.00", "50.00", "500.00")]
        assert [(i["item_id"], i["quantity"], i["rate"], i["amount"])
                for i in result["items"]] == [
            (env["item1"], "10.00", "50.00", "500.00")]
        for name in ("purchase_receipt", "purchase_receipt_item",
                     "audit_log"):
            table = Table(name)
            q = Q.from_(table).select(fn.Count("*").as_("n"))
            assert conn.execute(q.get_sql()).fetchone()["n"] == before[name]


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
        pi_id = create["purchase_invoice_id"]
        pi_t = Table("purchase_invoice")
        pii_t = Table("purchase_invoice_item")
        before = {}
        for name in ("purchase_invoice", "purchase_invoice_item",
                     "audit_log"):
            table = Table(name)
            q = Q.from_(table).select(fn.Count("*").as_("n"))
            before[name] = conn.execute(q.get_sql()).fetchone()["n"]
        result = call_action(mod.get_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "items" in result

        # Deepened (m478): get-purchase-invoice is read-only, so the response
        # must repeat the stored draft bill — 3 x 100.00 with no tax — and
        # its empty payment list exactly, and no table may gain a row.
        q = Q.from_(pi_t).select(pi_t.star).where(pi_t.id == P())
        stored = conn.execute(q.get_sql(), (pi_id,)).fetchone()
        assert (result["document_status"], result["total_amount"],
                result["tax_amount"], result["grand_total"],
                result["outstanding_amount"]) == (
            "draft", "300.00", "0.00", "300.00", "300.00")
        assert (stored["total_amount"], stored["tax_amount"],
                stored["grand_total"],
                stored["outstanding_amount"]) == (
            "300.00", "0.00", "300.00", "300.00")
        q = (Q.from_(pii_t)
             .select(pii_t.item_id, pii_t.quantity, pii_t.rate, pii_t.amount)
             .where(pii_t.purchase_invoice_id == P()))
        lines = conn.execute(q.get_sql(), (pi_id,)).fetchall()
        assert [(r["item_id"], r["quantity"], r["rate"], r["amount"])
                for r in lines] == [
            (env["item1"], "3.00", "100.00", "300.00")]
        assert [(i["item_id"], i["quantity"], i["rate"], i["amount"])
                for i in result["items"]] == [
            (env["item1"], "3.00", "100.00", "300.00")]
        assert result["payments"] == []
        for name in ("purchase_invoice", "purchase_invoice_item",
                     "audit_log"):
            table = Table(name)
            q = Q.from_(table).select(fn.Count("*").as_("n"))
            assert conn.execute(q.get_sql()).fetchone()["n"] == before[name]


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

        # Deepened (m478): the note is a negative draft against the bill —
        # 2 x 100.00 negated — the bill itself is untouched, and a draft
        # posts no ledger rows.
        dn_id = result["debit_note_id"]
        assert result["total_amount"] == "-200.00"
        pi_t = Table("purchase_invoice")
        q = Q.from_(pi_t).select(pi_t.star).where(pi_t.id == P())
        note = conn.execute(q.get_sql(), (dn_id,)).fetchone()
        assert note["status"] == "draft"
        assert note["is_return"] == 1
        assert note["return_against"] == create["purchase_invoice_id"]
        assert (note["total_amount"], note["tax_amount"],
                note["grand_total"],
                note["outstanding_amount"]) == (
            "-200.00", "0", "-200.00", "-200.00")
        pii_t = Table("purchase_invoice_item")
        q = (Q.from_(pii_t)
             .select(pii_t.item_id, pii_t.quantity, pii_t.rate, pii_t.amount)
             .where(pii_t.purchase_invoice_id == P()))
        lines = conn.execute(q.get_sql(), (dn_id,)).fetchall()
        assert [(r["item_id"], r["quantity"], r["rate"], r["amount"])
                for r in lines] == [
            (env["item1"], "-2.00", "100.00", "-200.00")]
        q = Q.from_(pi_t).select(pi_t.star).where(pi_t.id == P())
        orig = conn.execute(q.get_sql(),
                            (create["purchase_invoice_id"],)).fetchone()
        assert (orig["status"], orig["grand_total"],
                orig["outstanding_amount"]) == (
            "submitted", "500.00", "500.00")
        gl_t = Table("gl_entry")
        q = (Q.from_(gl_t).select(fn.Count("*").as_("n"))
             .where(gl_t.voucher_id == P()))
        assert conn.execute(q.get_sql(), (dn_id,)).fetchone()["n"] == 0


class TestUpdateInvoiceOutstandingRetired:
    """`update-purchase-outstanding` is RETIRED (steer shape, M776).

    The buying handler (``update_invoice_outstanding``, routed as
    ``update-purchase-outstanding``) moved a purchase invoice's
    ``outstanding_amount`` and appended a ``payment_ledger_entry`` adjustment
    row with NO general-ledger posting, so the GL and the sub-ledger drifted
    apart while the summary-versus-detail checks stayed green. No module
    calls it — payments clears documents in-process through
    ``erpclaw_lib.payment_clearing``.

    What is pinned here is the RETIREMENT CONTRACT (the M103 shape): the name
    stays ROUTABLE and answers with one JSON error naming the replacement
    flows, exit 1, never a traceback and never "Unknown action", and NOTHING
    LANDS. Against the pre-retirement handler this class is red on both
    halves — the old handler returned ``status: ok`` and wrote a ledger row.

    The two old tests pinned the retired action's own contract (INV-25 and
    INV-22 staying green after a GL-less balance move); that contract is
    withdrawn with the action — the checks now hold because the move cannot
    happen, and every sanctioned flow (add-payment -> submit-payment /
    allocate-payment, create-debit-note -> submit-purchase-invoice,
    write-off-invoice) posts both sides together.
    """

    def _submitted_invoice(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_purchase_invoice, conn, ns(
            purchase_order_id=None, purchase_receipt_id=None,
            supplier_id=env["supplier"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
        ))
        assert is_ok(create), f"invoice creation failed: {create}"
        submit = call_action(mod.submit_purchase_invoice, conn, ns(
            purchase_invoice_id=create["purchase_invoice_id"],
        ))
        assert is_ok(submit), f"invoice submit failed: {submit}"
        return create["purchase_invoice_id"]

    def test_retired_action_returns_a_steer_not_a_result(self, conn, env):
        """The retired action answers with one JSON error naming every
        replacement — the half that was red before M776 (``status: ok``)."""
        invoice_id = self._submitted_invoice(conn, env)
        result = call_action(mod.ACTIONS[RETIRED_KEY], conn, ns(
            purchase_invoice_id=invoice_id,
            amount="200.00",
        ))
        assert result["status"] == "error", (
            f"{PUBLIC_ACTION} still returned a result: "
            f"{json.dumps(result)[:300]}")
        assert "retired" in result["message"].lower(), result
        assert PUBLIC_ACTION in result["message"], (
            "the message must name the public action the caller typed")
        assert result["suggestion"] == payment_clearing.RETIRED_OUTSTANDING_STEER
        for replacement in SANCTIONED_FLOW:
            assert replacement in result["suggestion"], (
                f"the steer does not name {replacement}; a retirement without "
                f"a route is a dead end. Got: {result['suggestion']}")

    def _snapshot(self, conn, invoice_id):
        pi = conn.execute(
            "SELECT outstanding_amount, status FROM purchase_invoice WHERE id=?",
            (invoice_id,)).fetchone()
        ple_n = conn.execute(
            "SELECT COUNT(*) AS n FROM payment_ledger_entry").fetchone()["n"]
        gl_n = conn.execute(
            "SELECT COUNT(*) AS n FROM gl_entry").fetchone()["n"]
        audit_n = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]
        return {"outstanding_amount": pi["outstanding_amount"],
                "status": pi["status"],
                "payment_ledger_entry": ple_n,
                "gl_entry": gl_n,
                "audit_log": audit_n}

    def test_retired_action_writes_nothing(self, conn, env):
        """The invoice was submitted first, so both ledgers hold rows; the
        retired action must touch neither. The half that was red before M776:
        the old handler wrote a payment-ledger row here with no GL leg."""
        invoice_id = self._submitted_invoice(conn, env)
        before = self._snapshot(conn, invoice_id)
        assert before["payment_ledger_entry"] >= 1, (
            "the submitted invoice must own a payment-ledger row")
        assert before["gl_entry"] >= 1, (
            "the submitted invoice must own GL rows")
        call_action(mod.ACTIONS[RETIRED_KEY], conn, ns(
            purchase_invoice_id=invoice_id,
            amount="200.00",
        ))
        conn.commit()
        after = self._snapshot(conn, invoice_id)
        assert after == before, (
            f"{PUBLIC_ACTION} wrote something: {before} -> {after}")

    def test_full_legacy_invocation_reaches_the_steer_through_the_router(
            self, tmp_path):
        """Drive the FOUNDATION router exactly as a legacy caller would — the
        old flags included — and read what comes back: the name still routes
        (never "Unknown action"), the legacy flags still parse (an argparse
        usage error would exit 2 before the JSON contract), and what routes
        is the steer.

        Hermetic per the M54/M97 discipline: ERPCLAW_HOME is redirected at a
        temp dir so nothing touches the developer's real install, and
        PYTHONPATH binds the IN-TREE erpclaw_lib so find_spec resolves the
        tree under test rather than whatever the deployed symlink points at.
        The temp home gets a PROVISIONED database on purpose: the router's
        requires-setup pre-flight runs before dispatch, and the steer contract
        is about what an INSTALLED caller gets.
        """
        home = tmp_path / "home"
        (home / "lib").mkdir(parents=True)
        init_all_tables(str(home / "data.sqlite"))
        env = dict(os.environ, ERPCLAW_HOME=str(home), PYTHONPATH=_IN_TREE_LIB)
        proc = subprocess.run(
            [sys.executable, _ROUTER, "--action", PUBLIC_ACTION,
             "--purchase-invoice-id", str(uuid.uuid4()),
             "--amount", "200.00"],
            capture_output=True, text=True, env=env, timeout=120)

        assert proc.returncode == 1, (proc.returncode, proc.stdout[-400:],
                                      proc.stderr[-400:])
        assert "Traceback" not in proc.stderr, proc.stderr[-800:]
        payload = json.loads(proc.stdout)
        assert payload.get("status") == "error", payload
        assert "retired" in payload.get("message", "").lower(), payload
        assert PUBLIC_ACTION in payload.get("message", ""), payload
        for replacement in SANCTIONED_FLOW:
            assert replacement in payload.get("suggestion", ""), (
                replacement, payload)
