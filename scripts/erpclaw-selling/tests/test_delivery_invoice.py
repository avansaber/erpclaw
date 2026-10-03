"""Tests for erpclaw-selling delivery notes and sales invoices.

Actions tested:
  - create-delivery-note, get-delivery-note, list-delivery-notes
  - submit-delivery-note, cancel-delivery-note
  - create-sales-invoice, update-sales-invoice, get-sales-invoice
  - list-sales-invoices, submit-sales-invoice, cancel-sales-invoice
  - create-credit-note, list-credit-notes
  - update-invoice-outstanding is RETIRED (TestUpdateInvoiceOutstandingRetired)
"""
import json
import os
import subprocess
import sys
import uuid
from decimal import Decimal
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, init_all_tables,
)
from erpclaw_lib import payment_clearing

mod = load_db_query()


_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_DIR = os.path.dirname(_TESTS_DIR)
_SCRIPTS_DIR = os.path.dirname(_MODULE_DIR)
_ROUTER = os.path.join(_SCRIPTS_DIR, "db_query.py")
_IN_TREE_LIB = os.path.join(_SCRIPTS_DIR, "erpclaw-setup", "lib")

RETIRED_ACTION = "update-invoice-outstanding"

# The replacement routes. The steer must name ALL of them: the cash flow, the
# reduction flow, and the write-off — a retirement without a route is a dead
# end, and a partial route sends the caller somewhere that cannot finish the
# job they actually had.
SANCTIONED_FLOW = [
    "add-payment",
    "submit-payment",
    "allocate-payment",
    "create-credit-note",
    "write-off-invoice",
]


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate).
    Auto-adds warehouse_id for stock operations."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _create_confirmed_so(conn, env, items_str=None):
    """Helper: create and confirm a sales order."""
    items_str = items_str or _items(env, ("item1", "10", "100.00"))
    so = call_action(mod.add_sales_order, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-15", items=items_str,
        delivery_date="2026-07-01", tax_template_id=None,
    ))
    assert is_ok(so), f"SO creation failed: {so}"
    submit = call_action(mod.submit_sales_order, conn, ns(
        sales_order_id=so["sales_order_id"],
    ))
    assert is_ok(submit), f"SO submit failed: {submit}"
    return so["sales_order_id"]


# ──────────────────────────────────────────────────────────────────────────────
# Delivery Notes
# ──────────────────────────────────────────────────────────────────────────────

class TestCreateDeliveryNote:
    def test_create_from_so(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        result = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20",
            items=None,
        ))
        assert is_ok(result)
        assert "delivery_note_id" in result

        dn = conn.execute("SELECT * FROM delivery_note WHERE id=?",
                          (result["delivery_note_id"],)).fetchone()
        assert dn is not None
        assert dn["status"] == "draft"
        assert dn["sales_order_id"] == so_id

    def test_create_missing_so_fails(self, conn, env):
        result = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=None, posting_date="2026-06-20",
            items=None,
        ))
        assert is_error(result)

    def test_create_from_draft_so_fails(self, conn, env):
        items = _items(env, ("item1", "5", "50.00"))
        so = call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            delivery_date="2026-07-01", tax_template_id=None,
        ))
        result = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so["sales_order_id"], posting_date="2026-06-20",
            items=None,
        ))
        assert is_error(result)


class TestGetDeliveryNote:
    def test_get_with_items(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        dn = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20",
            items=None,
        ))
        result = call_action(mod.get_delivery_note, conn, ns(
            delivery_note_id=dn["delivery_note_id"],
        ))
        assert is_ok(result)
        assert "items" in result

    def test_get_nonexistent_fails(self, conn):
        result = call_action(mod.get_delivery_note, conn, ns(
            delivery_note_id="fake-id",
        ))
        assert is_error(result)


class TestListDeliveryNotes:
    def test_list(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20",
            items=None,
        ))
        result = call_action(mod.list_delivery_notes, conn, ns(
            company_id=env["company_id"], customer_id=None,
            sales_order_id=None, doc_status=None,
            from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSubmitDeliveryNote:
    def test_submit_posts_sle(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        dn = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20",
            items=None,
        ))
        result = call_action(mod.submit_delivery_note, conn, ns(
            delivery_note_id=dn["delivery_note_id"],
        ))
        assert is_ok(result)

        # DN should be marked submitted in DB
        row = conn.execute("SELECT status FROM delivery_note WHERE id=?",
                           (dn["delivery_note_id"],)).fetchone()
        assert row["status"] == "submitted"

        # SLE entries should have been posted
        assert result.get("sle_entries_created", 0) >= 1


class TestCancelDeliveryNote:
    def test_cancel_submitted(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        dn = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20",
            items=None,
        ))
        call_action(mod.submit_delivery_note, conn, ns(
            delivery_note_id=dn["delivery_note_id"],
        ))
        result = call_action(mod.cancel_delivery_note, conn, ns(
            delivery_note_id=dn["delivery_note_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM delivery_note WHERE id=?",
                           (dn["delivery_note_id"],)).fetchone()
        assert row["status"] == "cancelled"


# ──────────────────────────────────────────────────────────────────────────────
# Sales Invoices
# ──────────────────────────────────────────────────────────────────────────────

class TestCreateSalesInvoice:
    def test_create_from_so(self, conn, env):
        so_id = _create_confirmed_so(conn, env)
        result = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=so_id, delivery_note_id=None,
            customer_id=None, company_id=None,
            posting_date="2026-06-20", due_date=None,
            items=None, tax_template_id=None,
            payment_terms_id=None,
        ))
        assert is_ok(result)
        assert "sales_invoice_id" in result
        assert Decimal(result["grand_total"]) == Decimal("1000.00")

        si = conn.execute("SELECT * FROM sales_invoice WHERE id=?",
                          (result["sales_invoice_id"],)).fetchone()
        assert si["status"] == "draft"
        assert si["sales_order_id"] == so_id

    def test_create_standalone(self, conn, env):
        items = _items(env, ("item1", "5", "200.00"))
        result = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        assert is_ok(result)
        assert Decimal(result["grand_total"]) == Decimal("1000.00")

    def test_create_missing_customer_standalone_fails(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        result = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=None, company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        assert is_error(result)


class TestGetSalesInvoice:
    def test_get_with_items(self, conn, env):
        items = _items(env, ("item1", "3", "100.00"))
        create = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        result = call_action(mod.get_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        assert is_ok(result)
        assert "items" in result
        assert len(result["items"]) == 1


class TestListSalesInvoices:
    def test_list(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date=None,
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        result = call_action(mod.list_sales_invoices, conn, ns(
            company_id=env["company_id"], customer_id=None,
            sales_order_id=None, doc_status=None,
            from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSubmitSalesInvoice:
    def test_submit_posts_gl(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        result = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        assert is_ok(result)

        # SI should be marked submitted in DB
        si = conn.execute("SELECT status FROM sales_invoice WHERE id=?",
                          (create["sales_invoice_id"],)).fetchone()
        assert si["status"] == "submitted"

        # Should have posted GL entries
        gl_count = conn.execute(
            "SELECT COUNT(*) as cnt FROM gl_entry WHERE voucher_id=?",
            (create["sales_invoice_id"],)
        ).fetchone()["cnt"]
        assert gl_count >= 2  # At least AR debit + Revenue credit

    def test_submit_nonexistent_fails(self, conn):
        result = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id="fake-id",
        ))
        assert is_error(result)


class TestCancelSalesInvoice:
    def test_cancel_submitted(self, conn, env):
        items = _items(env, ("item1", "3", "100.00"))
        create = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        result = call_action(mod.cancel_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        assert is_ok(result)

        si = conn.execute("SELECT status FROM sales_invoice WHERE id=?",
                          (create["sales_invoice_id"],)).fetchone()
        assert si["status"] == "cancelled"


class TestCreateCreditNote:
    def test_credit_note_against_invoice(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        # Credit note requires items specifying return quantities
        return_items = json.dumps([
            {"item_id": env["item1"], "qty": "2", "rate": "100.00"}
        ])
        result = call_action(mod.create_credit_note, conn, ns(
            against_invoice_id=create["sales_invoice_id"],
            reason="Returned goods", posting_date="2026-06-25",
            items=return_items,
        ))
        assert is_ok(result)
        assert "credit_note_id" in result


class TestListCreditNotes:
    def test_list(self, conn, env):
        result = call_action(mod.list_credit_notes, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status=None, from_date=None, to_date=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)


class TestUpdateInvoiceOutstandingRetired:
    """`update-invoice-outstanding` is RETIRED (steer shape, M776).

    The action moved a sales invoice's ``outstanding_amount`` and appended a
    ``payment_ledger_entry`` adjustment row with NO general-ledger posting, so
    the GL and the sub-ledger drifted apart while the summary-versus-detail
    checks stayed green. No module calls it — payments clears documents
    in-process through ``erpclaw_lib.payment_clearing``.

    What is pinned here is the RETIREMENT CONTRACT (the M103 shape): the name
    stays ROUTABLE and answers with one JSON error naming the replacement
    flows, exit 1, never a traceback and never "Unknown action", and NOTHING
    LANDS. Against the pre-retirement handler this class is red on both
    halves — the old handler returned ``status: ok`` and wrote a ledger row.

    The two old tests pinned the retired action's own contract (INV-25 and
    INV-22 staying green after a GL-less balance move); that contract is
    withdrawn with the action — the checks now hold because the move cannot
    happen, and every sanctioned flow (add-payment -> submit-payment /
    allocate-payment, create-credit-note -> submit-sales-invoice,
    write-off-invoice) posts both sides together.
    """

    def _submitted_invoice(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.create_sales_invoice, conn, ns(
            sales_order_id=None, delivery_note_id=None,
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-20", due_date="2026-07-20",
            items=items, tax_template_id=None,
            payment_terms_id=None,
        ))
        assert is_ok(create), f"invoice creation failed: {create}"
        submit = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=create["sales_invoice_id"],
        ))
        assert is_ok(submit), f"invoice submit failed: {submit}"
        return create["sales_invoice_id"]

    def test_retired_action_returns_a_steer_not_a_result(self, conn, env):
        """The retired action answers with one JSON error naming every
        replacement — the half that was red before M776 (``status: ok``)."""
        invoice_id = self._submitted_invoice(conn, env)
        result = call_action(mod.ACTIONS[RETIRED_ACTION], conn, ns(
            sales_invoice_id=invoice_id,
            amount="200.00",
        ))
        assert result["status"] == "error", (
            f"{RETIRED_ACTION} still returned a result: "
            f"{json.dumps(result)[:300]}")
        assert "retired" in result["message"].lower(), result
        assert RETIRED_ACTION in result["message"], (
            "the message must name the action the caller typed")
        assert result["suggestion"] == payment_clearing.RETIRED_OUTSTANDING_STEER
        for replacement in SANCTIONED_FLOW:
            assert replacement in result["suggestion"], (
                f"the steer does not name {replacement}; a retirement without "
                f"a route is a dead end. Got: {result['suggestion']}")

    def _snapshot(self, conn, invoice_id):
        si = conn.execute(
            "SELECT outstanding_amount, status FROM sales_invoice WHERE id=?",
            (invoice_id,)).fetchone()
        ple_n = conn.execute(
            "SELECT COUNT(*) AS n FROM payment_ledger_entry").fetchone()["n"]
        gl_n = conn.execute(
            "SELECT COUNT(*) AS n FROM gl_entry").fetchone()["n"]
        audit_n = conn.execute(
            "SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]
        return {"outstanding_amount": si["outstanding_amount"],
                "status": si["status"],
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
        call_action(mod.ACTIONS[RETIRED_ACTION], conn, ns(
            sales_invoice_id=invoice_id,
            amount="200.00",
        ))
        conn.commit()
        after = self._snapshot(conn, invoice_id)
        assert after == before, (
            f"{RETIRED_ACTION} wrote something: {before} -> {after}")

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
            [sys.executable, _ROUTER, "--action", RETIRED_ACTION,
             "--sales-invoice-id", str(uuid.uuid4()),
             "--amount", "200.00"],
            capture_output=True, text=True, env=env, timeout=120)

        assert proc.returncode == 1, (proc.returncode, proc.stdout[-400:],
                                      proc.stderr[-400:])
        assert "Traceback" not in proc.stderr, proc.stderr[-800:]
        payload = json.loads(proc.stdout)
        assert payload.get("status") == "error", payload
        assert "retired" in payload.get("message", "").lower(), payload
        assert RETIRED_ACTION in payload.get("message", ""), payload
        for replacement in SANCTIONED_FLOW:
            assert replacement in payload.get("suggestion", ""), (
                replacement, payload)
