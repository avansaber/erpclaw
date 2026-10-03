"""Strong-depth tests for 12 weak behavioural tests (m325-strong-depth-selling-1).

Each action below already has a behavioural test elsewhere in this directory;
the depth instrument flags those tests weak (content-only assertions: no money
literal, no read-back through the seam, no pinned refusal). This module deepens
each one WITHOUT deleting or weakening the original. Every test docstring names
the existing test it deepens and states which assertion now carries the weight.

Strong contract per action:
  1. Read back through the seam (SELECT the rows, compare EXACT values).
  2. A hand-computed money literal wherever money exists (exact strings).
  3. A pinned refusal: refusal asserted, message truthful, DB unchanged.
  4. Explicit NOT-changed assertions (counts + re-reads of neighbours).

Scope note: the audit trail and naming-series counters advance by design on
every action, so the unchanged snapshots below cover domain tables only.
No sqlite_master / PRAGMA / information_schema is used anywhere here.
"""
import json
from decimal import Decimal
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_customer,
)

mod = load_db_query()

ALL_TABLES = [
    "customer", "quotation", "quotation_item",
    "sales_order", "sales_order_item",
    "delivery_note", "delivery_note_item",
    "sales_invoice", "sales_invoice_item",
    "purchase_invoice", "purchase_invoice_item",
    "blanket_order", "blanket_order_item",
    "dunning_level", "dunning_run",
    "sales_partner", "packing_slip", "packing_slip_item",
    "payment_ledger_entry", "gl_entry", "stock_ledger_entry",
]


def _counts(conn):
    return {t: conn.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
            for t in ALL_TABLES}


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


def _items_nowh(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r}
        for k, q, r in specs
    ])


def _confirmed_so(conn, env, qty="10", rate="100.00"):
    so = call_action(mod.add_sales_order, conn, ns(
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-15", items=_items(env, ("item1", qty, rate)),
        delivery_date="2026-07-01", tax_template_id=None,
    ))
    assert is_ok(so), so
    submit = call_action(mod.submit_sales_order, conn, ns(
        sales_order_id=so["sales_order_id"]))
    assert is_ok(submit), submit
    return so["sales_order_id"]


def _standalone_si(conn, env, qty="5", rate="100.00"):
    created = call_action(mod.create_sales_invoice, conn, ns(
        sales_order_id=None, delivery_note_id=None,
        customer_id=env["customer"], company_id=env["company_id"],
        posting_date="2026-06-20", due_date="2026-07-20",
        items=_items(env, ("item1", qty, rate)),
        tax_template_id=None, payment_terms_id=None,
    ))
    assert is_ok(created), created
    return created["sales_invoice_id"]


# ---------------------------------------------------------------------------
# 1. add-dunning-level
# ---------------------------------------------------------------------------

class TestAddDunningLevelStrong:
    def test_strong_readback(self, conn, env):
        """Deepens TestAddDunningLevel.test_basic (test_credit_dunning.py),
        which asserts only level/action on the response. The seam read-back
        of the exact dunning_level row below carries the weight."""
        before = _counts(conn)
        cust_before = dict(conn.execute(
            "SELECT credit_status, credit_limit FROM customer WHERE id=?",
            (env["customer"],)).fetchone())
        result = call_action(mod.add_dunning_level, conn, ns(
            company_id=env["company_id"],
            level=3, days_overdue=45,
            dunning_action="hold",
            template_id="TPL-REM-3", description="Third reminder",
        ))
        assert is_ok(result)
        assert result["level"] == 3
        assert result["days_overdue"] == 45
        assert result["action"] == "hold"
        row = conn.execute(
            "SELECT company_id, level, days_overdue, action, template_id,"
            " description FROM dunning_level WHERE id=?",
            (result["id"],)).fetchone()
        assert row is not None
        assert row["company_id"] == env["company_id"]
        assert row["level"] == 3
        assert row["days_overdue"] == 45
        assert row["action"] == "hold"
        assert row["template_id"] == "TPL-REM-3"
        assert row["description"] == "Third reminder"
        # No money exists on a dunning level (escalation config only).
        after = _counts(conn)
        assert after["dunning_level"] == before["dunning_level"] + 1
        for t in ALL_TABLES:
            if t == "dunning_level":
                continue
            assert after[t] == before[t], t
        assert dict(conn.execute(
            "SELECT credit_status, credit_limit FROM customer WHERE id=?",
            (env["customer"],)).fetchone()) == cust_before

    def test_refusal_duplicate_unchanged(self, conn, env):
        """Pins the duplicate-level refusal: truthful message, original row
        and every other table unchanged."""
        first = call_action(mod.add_dunning_level, conn, ns(
            company_id=env["company_id"],
            level=2, days_overdue=60,
            dunning_action="hold",
            template_id=None, description=None,
        ))
        assert is_ok(first)
        orig = dict(conn.execute(
            "SELECT company_id, level, days_overdue, action FROM dunning_level"
            " WHERE id=?", (first["id"],)).fetchone())
        before = _counts(conn)
        refused = call_action(mod.add_dunning_level, conn, ns(
            company_id=env["company_id"],
            level=2, days_overdue=90,
            dunning_action="call",
            template_id=None, description=None,
        ))
        assert is_error(refused)
        assert "already exists" in refused["message"]
        conn.rollback()
        assert _counts(conn) == before
        assert dict(conn.execute(
            "SELECT company_id, level, days_overdue, action FROM dunning_level"
            " WHERE id=?", (first["id"],)).fetchone()) == orig


# ---------------------------------------------------------------------------
# 2. add-sales-partner
# ---------------------------------------------------------------------------

class TestAddSalesPartnerStrong:
    def test_strong_readback(self, conn, env):
        """Deepens TestAddSalesPartner.test_basic_create
        (test_misc_selling.py), which asserts only that an id was returned.
        The seam read-back of name/commission_rate carries the weight."""
        before = _counts(conn)
        result = call_action(mod.add_sales_partner, conn, ns(
            name="Channel Partner A", company_id=env["company_id"],
            commission_rate="10.00",
        ))
        assert is_ok(result)
        assert result["name"] == "Channel Partner A"
        assert result["commission_rate"] == "10.00"
        row = conn.execute(
            "SELECT name, commission_rate FROM sales_partner WHERE id=?",
            (result["sales_partner_id"],)).fetchone()
        assert row is not None
        assert row["name"] == "Channel Partner A"
        assert row["commission_rate"] == "10.00"
        after = _counts(conn)
        assert after["sales_partner"] == before["sales_partner"] + 1
        for t in ALL_TABLES:
            if t == "sales_partner":
                continue
            assert after[t] == before[t], t

    def test_refusal_missing_name_unchanged(self, conn, env):
        """Pins the missing-name refusal with the partner table unchanged."""
        before = _counts(conn)
        refused = call_action(mod.add_sales_partner, conn, ns(
            name=None, company_id=env["company_id"],
            commission_rate="5.00",
        ))
        assert is_error(refused)
        assert "--name is required" in refused["message"]
        assert _counts(conn) == before


# ---------------------------------------------------------------------------
# 3. create-intercompany-invoice
# ---------------------------------------------------------------------------

class TestCreateIntercompanyInvoiceStrong:
    def test_refusal_draft_must_submit_unchanged(self, conn, env):
        """Pins the draft-invoice refusal. Hand-computed money: 10 x 100.00
        = 1000.00; the unchanged grand_total/outstanding literals below
        carry the weight."""
        si_id = _standalone_si(conn, env, qty="10", rate="100.00")
        before = _counts(conn)
        refused = call_action(mod.create_intercompany_invoice, conn, ns(
            sales_invoice_id=si_id,
            target_company_id="other-company-id",
            supplier_id="some-supplier-id",
        ))
        assert is_error(refused)
        assert "must be submitted" in refused["message"]
        assert "draft" in refused["message"]
        assert _counts(conn) == before
        si = conn.execute(
            "SELECT status, total_amount, grand_total, outstanding_amount"
            " FROM sales_invoice WHERE id=?", (si_id,)).fetchone()
        assert si["status"] == "draft"
        assert si["total_amount"] == "1000.00"
        assert si["grand_total"] == "1000.00"
        assert si["outstanding_amount"] == "1000.00"

    def test_refusal_missing_id_unchanged(self, conn, env):
        """Pins the missing-argument refusal with zero mirror rows."""
        before = _counts(conn)
        refused = call_action(mod.create_intercompany_invoice, conn, ns(
            sales_invoice_id=None, target_company_id=None, supplier_id=None,
        ))
        assert is_error(refused)
        assert "--sales-invoice-id is required" in refused["message"]
        assert _counts(conn) == before
        assert before["purchase_invoice"] == 0

    def test_unknown_target_company_refused(self, conn, env):
        """Pins the unknown-target-company refusal: truthful message, no
        mirror row, SI untouched. Hand-computed money: 10 x 100.00 =
        1000.00; the unchanged grand_total/outstanding literals below
        carry the weight."""
        si_id = _standalone_si(conn, env, qty="10", rate="100.00")
        submitted = call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=si_id))
        assert is_ok(submitted)
        assert conn.execute(
            "SELECT grand_total FROM sales_invoice WHERE id=?",
            (si_id,)).fetchone()["grand_total"] == "1000.00"
        before = _counts(conn)
        refused = call_action(mod.create_intercompany_invoice, conn, ns(
            sales_invoice_id=si_id,
            target_company_id="target-company-id",
            supplier_id="supplier-id",
        ))
        assert is_error(refused)
        assert refused["message"] == "Target company not found: target-company-id"
        assert _counts(conn) == before
        si = conn.execute(
            "SELECT status, grand_total, outstanding_amount"
            " FROM sales_invoice WHERE id=?", (si_id,)).fetchone()
        assert si["status"] == "submitted"
        assert si["grand_total"] == "1000.00"
        assert si["outstanding_amount"] == "1000.00"


# ---------------------------------------------------------------------------
# 4. get-amendment-history
# ---------------------------------------------------------------------------

class TestGetAmendmentHistoryStrong:
    def test_strong_chain(self, conn, env):
        """Deepens TestGetAmendmentHistory.test_get_amendment_history
        (test_close_amend_so.py). Hand-computed money: 10 x 100.00 = 1000.00
        on both generations. The per-generation grand_total literals plus the
        seam cross-check of both sales_order rows carry the weight."""
        so1 = _confirmed_so(conn, env, qty="10", rate="100.00")
        r1 = call_action(mod.amend_sales_order, conn, ns(
            sales_order_id=so1, items=None))
        assert is_ok(r1)
        so2 = r1["new_sales_order_id"]
        assert is_ok(call_action(mod.submit_sales_order, conn, ns(
            sales_order_id=so2)))
        before = _counts(conn)
        hist = call_action(mod.get_amendment_history, conn, ns(
            sales_order_id=so2))
        assert is_ok(hist)
        assert hist["chain_length"] == 2
        chain = hist["amendment_chain"]
        assert [e["sales_order_id"] for e in chain] == [so1, so2]
        assert chain[0]["status"] == "cancelled"
        assert chain[0]["amended_from"] is None
        assert chain[0]["grand_total"] == "1000.00"
        assert chain[1]["status"] == "confirmed"
        assert chain[1]["amended_from"] == so1
        assert chain[1]["grand_total"] == "1000.00"
        for sid, status in ((so1, "cancelled"), (so2, "confirmed")):
            row = conn.execute(
                "SELECT status, grand_total, amended_from FROM sales_order"
                " WHERE id=?", (sid,)).fetchone()
            assert row["status"] == status
            assert row["grand_total"] == "1000.00"
        # Read-only: the lookup wrote nothing.
        assert _counts(conn) == before

    def test_refusal_not_found_unchanged(self, conn, env):
        """Pins the not-found refusal with the order tables unchanged."""
        _confirmed_so(conn, env)
        before = _counts(conn)
        refused = call_action(mod.get_amendment_history, conn, ns(
            sales_order_id="no-such-order"))
        assert is_error(refused)
        assert "not found" in refused["message"].lower()
        assert _counts(conn) == before


# ---------------------------------------------------------------------------
# 5. get-blanket-order
# ---------------------------------------------------------------------------

class TestGetBlanketOrderStrong:
    def _two_item_blanket(self, conn, env):
        items = _items_nowh(env, ("item1", "100", "50.00"),
                            ("item2", "50", "100.00"))
        created = call_action(mod.add_blanket_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            items=items, valid_from="2026-01-01", valid_to="2027-12-31",
            blanket_order_id=None, doc_status=None,
            tax_template_id=None, posting_date=None, delivery_date=None,
            valid_till=None, name=None,
        ))
        assert is_ok(created), created
        return created["blanket_order_id"]

    def test_strong_readback(self, conn, env):
        """Deepens TestGetBlanketOrder.test_get_with_items
        (test_blanket_order.py), which asserts only type and item count.
        Hand-computed money: 100 x 50.00 = 5000.00 and 50 x 100.00 =
        5000.00 (total 10000.00, total_qty 150). The exact item literals
        plus the seam cross-check carry the weight."""
        bo_id = self._two_item_blanket(conn, env)
        before = _counts(conn)
        result = call_action(mod.get_blanket_order, conn, ns(
            blanket_order_id=bo_id))
        assert is_ok(result)
        assert result["customer_id"] == env["customer"]
        assert result["blanket_order_type"] == "selling"
        assert result["total_qty"] == "150.00"
        assert result["valid_from"] == "2026-01-01"
        assert result["valid_to"] == "2027-12-31"
        assert len(result["items"]) == 2
        got = {i["item_id"]: i for i in result["items"]}
        assert got[env["item1"]]["quantity"] == "100.00"
        assert got[env["item1"]]["rate"] == "50.00"
        assert got[env["item1"]]["amount"] == "5000.00"
        assert got[env["item2"]]["quantity"] == "50.00"
        assert got[env["item2"]]["rate"] == "100.00"
        assert got[env["item2"]]["amount"] == "5000.00"
        db_items = {r["item_id"]: dict(r) for r in conn.execute(
            "SELECT item_id, quantity, rate, amount FROM blanket_order_item"
            " WHERE blanket_order_id=?", (bo_id,)).fetchall()}
        assert db_items[env["item1"]]["amount"] == "5000.00"
        assert db_items[env["item2"]]["amount"] == "5000.00"
        assert sum(Decimal(r["amount"]) for r in db_items.values()) == \
            Decimal("10000.00")
        assert conn.execute(
            "SELECT total_qty FROM blanket_order WHERE id=?",
            (bo_id,)).fetchone()["total_qty"] == "150.00"
        # Read-only: the lookup wrote nothing.
        assert _counts(conn) == before

    def test_refusal_not_found_unchanged(self, conn, env):
        """Pins the not-found refusal with blanket tables unchanged."""
        self._two_item_blanket(conn, env)
        before = _counts(conn)
        refused = call_action(mod.get_blanket_order, conn, ns(
            blanket_order_id="no-such-blanket"))
        assert is_error(refused)
        assert "not found" in refused["message"].lower()
        assert _counts(conn) == before


# ---------------------------------------------------------------------------
# 6. get-delivery-note
# ---------------------------------------------------------------------------

class TestGetDeliveryNoteStrong:
    def test_strong_readback(self, conn, env):
        """Deepens TestGetDeliveryNote.test_get_with_items
        (test_delivery_invoice.py), which asserts only that items exist.
        Hand-computed money: 10 x 100.00 = 1000.00. The exact item
        literals plus the seam cross-check carry the weight."""
        so_id = _confirmed_so(conn, env, qty="10", rate="100.00")
        dn = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20", items=None))
        assert is_ok(dn), dn
        dn_id = dn["delivery_note_id"]
        before = _counts(conn)
        result = call_action(mod.get_delivery_note, conn, ns(
            delivery_note_id=dn_id))
        assert is_ok(result)
        assert result["customer_id"] == env["customer"]
        assert result["sales_order_id"] == so_id
        assert result["total_qty"] == "10.00"
        assert result["document_status"] == "draft"
        assert len(result["items"]) == 1
        item = result["items"][0]
        assert item["quantity"] == "10.00"
        assert item["rate"] == "100.00"
        assert item["amount"] == "1000.00"
        assert item["warehouse_id"] == env["warehouse"]
        assert item["item_name"] == "Widget A"
        db_item = conn.execute(
            "SELECT quantity, rate, amount, warehouse_id FROM delivery_note_item"
            " WHERE delivery_note_id=?", (dn_id,)).fetchone()
        assert db_item["quantity"] == "10.00"
        assert db_item["rate"] == "100.00"
        assert db_item["amount"] == "1000.00"
        assert db_item["warehouse_id"] == env["warehouse"]
        # Read-only: the lookup wrote nothing.
        assert _counts(conn) == before

    def test_refusal_not_found_unchanged(self, conn, env):
        """Pins the not-found refusal with delivery tables unchanged."""
        so_id = _confirmed_so(conn, env)
        assert is_ok(call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20", items=None)))
        before = _counts(conn)
        refused = call_action(mod.get_delivery_note, conn, ns(
            delivery_note_id="no-such-dn"))
        assert is_error(refused)
        assert "not found" in refused["message"].lower()
        assert _counts(conn) == before


# ---------------------------------------------------------------------------
# 7. get-quotation
# ---------------------------------------------------------------------------

class TestGetQuotationStrong:
    def test_strong_readback(self, conn, env):
        """Deepens TestGetQuotation.test_get_with_items (test_quotation.py),
        which asserts only that one item exists. Hand-computed money:
        3 x 50.00 = 150.00. The exact total/item literals plus the seam
        cross-check carry the weight."""
        created = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15",
            items=_items_nowh(env, ("item1", "3", "50.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(created), created
        q_id = created["quotation_id"]
        before = _counts(conn)
        result = call_action(mod.get_quotation, conn, ns(quotation_id=q_id))
        assert is_ok(result)
        assert result["customer_id"] == env["customer"]
        assert result["total_amount"] == "150.00"
        assert result["grand_total"] == "150.00"
        assert len(result["items"]) == 1
        assert result["items"][0]["quantity"] == "3.00"
        assert result["items"][0]["rate"] == "50.00"
        assert result["items"][0]["amount"] == "150.00"
        db_item = conn.execute(
            "SELECT quantity, rate, amount FROM quotation_item"
            " WHERE quotation_id=?", (q_id,)).fetchone()
        assert db_item["quantity"] == "3.00"
        assert db_item["rate"] == "50.00"
        assert db_item["amount"] == "150.00"
        assert conn.execute(
            "SELECT total_amount, grand_total FROM quotation WHERE id=?",
            (q_id,)).fetchone()["grand_total"] == "150.00"
        # Read-only: the lookup wrote nothing.
        assert _counts(conn) == before

    def test_refusal_not_found_unchanged(self, conn, env):
        """Pins the not-found refusal with quotation tables unchanged."""
        created = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15",
            items=_items_nowh(env, ("item1", "1", "10.00")),
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(created)
        before = _counts(conn)
        refused = call_action(mod.get_quotation, conn, ns(
            quotation_id="no-such-quotation"))
        assert is_error(refused)
        assert "not found" in refused["message"].lower()
        assert _counts(conn) == before


# ---------------------------------------------------------------------------
# 8. get-sales-invoice
# ---------------------------------------------------------------------------

class TestGetSalesInvoiceStrong:
    def test_strong_draft_then_submitted(self, conn, env):
        """Deepens test_get_sales_invoice_reports_draft_then_submitted
        (test_get_sales_invoice_status.py), which asserts only the status
        flip. Hand-computed money: 5 x 100.00 = 500.00. The exact
        total/outstanding/item literals plus the seam cross-check carry
        the weight."""
        si_id = _standalone_si(conn, env, qty="5", rate="100.00")
        before = _counts(conn)
        draft = call_action(mod.get_sales_invoice, conn, ns(
            sales_invoice_id=si_id))
        assert is_ok(draft)
        assert draft["document_status"] == "draft"
        assert draft["total_amount"] == "500.00"
        assert draft["grand_total"] == "500.00"
        assert draft["outstanding_amount"] == "500.00"
        assert len(draft["items"]) == 1
        assert draft["items"][0]["quantity"] == "5.00"
        assert draft["items"][0]["rate"] == "100.00"
        assert draft["items"][0]["amount"] == "500.00"
        db_item = conn.execute(
            "SELECT quantity, rate, amount FROM sales_invoice_item"
            " WHERE sales_invoice_id=?", (si_id,)).fetchone()
        assert db_item["quantity"] == "5.00"
        assert db_item["rate"] == "100.00"
        assert db_item["amount"] == "500.00"
        assert _counts(conn) == before
        assert is_ok(call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=si_id)))
        mid = _counts(conn)
        fetched = call_action(mod.get_sales_invoice, conn, ns(
            sales_invoice_id=si_id))
        assert is_ok(fetched)
        assert fetched["document_status"] == "submitted"
        assert fetched["grand_total"] == "500.00"
        assert fetched["outstanding_amount"] == "500.00"
        assert fetched["items"][0]["amount"] == "500.00"
        assert conn.execute(
            "SELECT status, outstanding_amount FROM sales_invoice WHERE id=?",
            (si_id,)).fetchone()["outstanding_amount"] == "500.00"
        # Read-only: the lookups wrote nothing; submit is the only writer.
        assert _counts(conn) == mid

    def test_refusal_not_found_unchanged(self, conn, env):
        """Pins the not-found refusal with invoice tables unchanged."""
        _standalone_si(conn, env)
        before = _counts(conn)
        refused = call_action(mod.get_sales_invoice, conn, ns(
            sales_invoice_id="no-such-invoice"))
        assert is_error(refused)
        assert "not found" in refused["message"].lower()
        assert _counts(conn) == before


# ---------------------------------------------------------------------------
# 9. list-blanket-orders
# ---------------------------------------------------------------------------

class TestListBlanketOrdersStrong:
    def _blanket(self, conn, env, item_key, qty, rate, customer=None):
        items = _items_nowh(env, (item_key, qty, rate))
        created = call_action(mod.add_blanket_order, conn, ns(
            customer_id=customer or env["customer"],
            company_id=env["company_id"],
            items=items, valid_from="2026-01-01", valid_to="2027-12-31",
            blanket_order_id=None, doc_status=None,
            tax_template_id=None, posting_date=None, delivery_date=None,
            valid_till=None, name=None,
        ))
        assert is_ok(created), created
        return created["blanket_order_id"]

    def test_strong_exact_listing(self, conn, env):
        """Deepens TestListBlanketOrders.test_list (test_blanket_order.py),
        which asserts only a count floor and the type flag. Hand-computed
        quantities: 100 and 50. The exact total_qty literals per blanket
        plus the negative doc_status filter carry the weight."""
        bo1 = self._blanket(conn, env, "item1", "100", "50.00")
        bo2 = self._blanket(conn, env, "item2", "50", "100.00")
        before = _counts(conn)
        result = call_action(mod.list_blanket_orders, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status=None, limit="20", offset="0",
        ))
        assert is_ok(result)
        assert result["total_count"] == 2
        assert result["has_more"] is False
        got = {b["id"]: b for b in result["blanket_orders"]}
        assert set(got) == {bo1, bo2}
        assert got[bo1]["customer_id"] == env["customer"]
        assert got[bo1]["total_qty"] == "100.00"
        assert got[bo1]["blanket_order_type"] == "selling"
        assert got[bo2]["customer_id"] == env["customer"]
        assert got[bo2]["total_qty"] == "50.00"
        assert got[bo2]["blanket_order_type"] == "selling"
        # Money lives on the blanket items: 100 x 50.00 = 5000.00.
        assert conn.execute(
            "SELECT amount FROM blanket_order_item WHERE blanket_order_id=?"
            " AND item_id=?", (bo1, env["item1"])).fetchone()["amount"] == \
            "5000.00"
        # Truthful emptiness: both are drafts, so no active blanket exists.
        active = call_action(mod.list_blanket_orders, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status="active", limit="20", offset="0",
        ))
        assert is_ok(active)
        assert active["total_count"] == 0
        assert active["blanket_orders"] == []
        # Read-only: listings wrote nothing.
        assert _counts(conn) == before

    def test_refusal_isolation_foreign_company(self, conn, env):
        """Pinned refusal for list-blanket-orders: the action exposes no
        error-return path (every filter is optional), so the refusal it must
        honour is cross-scope isolation — a foreign company sees zero rows,
        a truthful total_count of 0, and the DB is unchanged."""
        self._blanket(conn, env, "item1", "100", "50.00")
        other_company = seed_company(conn)
        seed_customer(conn, other_company, "Foreign Customer")
        before = _counts(conn)
        result = call_action(mod.list_blanket_orders, conn, ns(
            company_id=other_company, customer_id=None,
            doc_status=None, limit="20", offset="0",
        ))
        assert is_ok(result)
        assert result["total_count"] == 0
        assert result["blanket_orders"] == []
        assert _counts(conn) == before


# ---------------------------------------------------------------------------
# 10. list-credit-notes
# ---------------------------------------------------------------------------

class TestListCreditNotesStrong:
    def test_strong_exact_listing(self, conn, env):
        """Deepens TestListCreditNotes.test_list (test_delivery_invoice.py),
        which asserts only ok. Hand-computed money: invoice 5 x 100.00 =
        500.00; credit note 2 x 100.00 = -200.00. The exact negative
        literals, the exclusion of the original invoice, and the untouched
        500.00 outstanding carry the weight."""
        si_id = _standalone_si(conn, env, qty="5", rate="100.00")
        assert is_ok(call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=si_id)))
        cn = call_action(mod.create_credit_note, conn, ns(
            against_invoice_id=si_id, reason="Returned goods",
            posting_date="2026-06-25",
            items=json.dumps([{"item_id": env["item1"], "qty": "2",
                               "rate": "100.00"}]),
        ))
        assert is_ok(cn), cn
        cn_id = cn["credit_note_id"]
        before = _counts(conn)
        result = call_action(mod.list_credit_notes, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status=None, from_date=None, to_date=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] == 1
        row = result["credit_notes"][0]
        assert row["id"] == cn_id
        assert row["customer_id"] == env["customer"]
        assert row["customer_name"] == "Acme Corp"
        assert row["grand_total"] == "-200.00"
        assert row["outstanding_amount"] == "-200.00"
        assert row["status"] == "draft"
        # The original invoice must NOT appear: the is_return filter is exact.
        assert si_id not in [r["id"] for r in result["credit_notes"]]
        assert conn.execute(
            "SELECT COUNT(*) FROM sales_invoice WHERE is_return=1"
            ).fetchone()[0] == 1
        # The original keeps its full outstanding by design.
        assert conn.execute(
            "SELECT outstanding_amount FROM sales_invoice WHERE id=?",
            (si_id,)).fetchone()["outstanding_amount"] == "500.00"
        # Truthful emptiness: the credit note is a draft, not submitted.
        submitted = call_action(mod.list_credit_notes, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status="submitted", from_date=None, to_date=None,
            limit=None, offset=None,
        ))
        assert is_ok(submitted)
        assert submitted["total_count"] == 0
        # Read-only: listings wrote nothing.
        assert _counts(conn) == before

    def test_refusal_isolation_foreign_company(self, conn, env):
        """Pinned refusal for list-credit-notes: no error-return path exists
        (every filter is optional), so the refusal is cross-scope isolation
        — a foreign company sees zero rows with total_count 0, DB unchanged."""
        si_id = _standalone_si(conn, env, qty="5", rate="100.00")
        assert is_ok(call_action(mod.submit_sales_invoice, conn, ns(
            sales_invoice_id=si_id)))
        assert is_ok(call_action(mod.create_credit_note, conn, ns(
            against_invoice_id=si_id, reason="Returned goods",
            posting_date="2026-06-25",
            items=json.dumps([{"item_id": env["item1"], "qty": "2",
                               "rate": "100.00"}]),
        )))
        other_company = seed_company(conn)
        seed_customer(conn, other_company, "Foreign Customer")
        before = _counts(conn)
        result = call_action(mod.list_credit_notes, conn, ns(
            company_id=other_company, customer_id=None,
            doc_status=None, from_date=None, to_date=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] == 0
        assert result["credit_notes"] == []
        assert _counts(conn) == before


# ---------------------------------------------------------------------------
# 11. list-delivery-notes
# ---------------------------------------------------------------------------

class TestListDeliveryNotesStrong:
    def test_strong_exact_listing(self, conn, env):
        """Deepens TestListDeliveryNotes.test_list (test_delivery_invoice.py),
        which asserts only a count floor. Hand-computed money: 10 x 100.00
        = 1000.00 on the note item. The exact row literals plus the seam
        read-back of the item amount carry the weight."""
        so_id = _confirmed_so(conn, env, qty="10", rate="100.00")
        dn = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20", items=None))
        assert is_ok(dn), dn
        dn_id = dn["delivery_note_id"]
        before = _counts(conn)
        result = call_action(mod.list_delivery_notes, conn, ns(
            company_id=env["company_id"], customer_id=None,
            sales_order_id=None, doc_status=None,
            from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] == 1
        row = result["delivery_notes"][0]
        assert row["id"] == dn_id
        assert row["customer_id"] == env["customer"]
        assert row["customer_name"] == "Acme Corp"
        assert row["sales_order_id"] == so_id
        assert row["total_qty"] == "10.00"
        assert row["status"] == "draft"
        db_item = conn.execute(
            "SELECT quantity, rate, amount FROM delivery_note_item"
            " WHERE delivery_note_id=?", (dn_id,)).fetchone()
        assert db_item["quantity"] == "10.00"
        assert db_item["rate"] == "100.00"
        assert db_item["amount"] == "1000.00"
        # Truthful emptiness: the note is a draft, not submitted.
        submitted = call_action(mod.list_delivery_notes, conn, ns(
            company_id=env["company_id"], customer_id=None,
            sales_order_id=None, doc_status="submitted",
            from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(submitted)
        assert submitted["total_count"] == 0
        # Read-only: listings wrote nothing.
        assert _counts(conn) == before

    def test_refusal_isolation_foreign_company(self, conn, env):
        """Pinned refusal for list-delivery-notes: no error-return path
        exists (every filter is optional), so the refusal is cross-scope
        isolation — a foreign company sees zero rows, DB unchanged."""
        so_id = _confirmed_so(conn, env)
        assert is_ok(call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20", items=None)))
        other_company = seed_company(conn)
        seed_customer(conn, other_company, "Foreign Customer")
        before = _counts(conn)
        result = call_action(mod.list_delivery_notes, conn, ns(
            company_id=other_company, customer_id=None,
            sales_order_id=None, doc_status=None,
            from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] == 0
        assert result["delivery_notes"] == []
        assert _counts(conn) == before


# ---------------------------------------------------------------------------
# 12. list-packing-slips
# ---------------------------------------------------------------------------

class TestListPackingSlipsStrong:
    def _dn_with_item(self, conn, env):
        so_id = _confirmed_so(conn, env, qty="10", rate="100.00")
        dn = call_action(mod.create_delivery_note, conn, ns(
            sales_order_id=so_id, posting_date="2026-06-20", items=None))
        assert is_ok(dn), dn
        dni = conn.execute(
            "SELECT id, quantity FROM delivery_note_item"
            " WHERE delivery_note_id=?", (dn["delivery_note_id"],)).fetchone()
        return dn["delivery_note_id"], dni["id"]

    def test_strong_exact_listing(self, conn, env):
        """Deepens TestPackingSlip.test_list_packing_slips
        (test_dropship_packing.py), which asserts only count == 1. Packing
        slips carry no money columns (quantities only), so the hand-computed
        literals are quantities: packed 4.00 of 10.00. The exact row literals
        plus the seam read-back of qty_packed carry the weight."""
        dn_id, dni_id = self._dn_with_item(conn, env)
        slip = call_action(mod.add_packing_slip, conn, ns(
            delivery_note_id=dn_id,
            items=json.dumps([{"delivery_note_item_id": dni_id,
                               "qty_packed": "4"}]),
            posting_date="2026-06-15", notes=None, reason=None,
            company_id=env["company_id"],
        ))
        assert is_ok(slip), slip
        ps_id = slip["packing_slip_id"]
        before = _counts(conn)
        result = call_action(mod.list_packing_slips, conn, ns(
            delivery_note_id=dn_id, company_id=None,
            limit="20", offset="0",
        ))
        assert is_ok(result)
        assert result["count"] == 1
        row = result["packing_slips"][0]
        assert row["id"] == ps_id
        assert row["delivery_note_id"] == dn_id
        assert row["company_id"] == env["company_id"]
        assert row["posting_date"] == "2026-06-15"
        fetched = call_action(mod.get_packing_slip, conn, ns(
            packing_slip_id=ps_id))
        assert is_ok(fetched)
        assert fetched["items"][0]["qty_packed"] == "4.00"
        assert conn.execute(
            "SELECT qty_packed FROM packing_slip_item"
            " WHERE packing_slip_id=?", (ps_id,)).fetchone()["qty_packed"] == \
            "4.00"
        # The slip must NOT mutate the delivery note item (still 10.00).
        assert conn.execute(
            "SELECT quantity, amount FROM delivery_note_item WHERE id=?",
            (dni_id,)).fetchone()["quantity"] == "10.00"
        # Truthful emptiness: an unknown delivery note has no slips.
        empty = call_action(mod.list_packing_slips, conn, ns(
            delivery_note_id="no-such-dn", company_id=None,
            limit="20", offset="0",
        ))
        assert is_ok(empty)
        assert empty["count"] == 0
        assert empty["packing_slips"] == []
        # Read-only: listings wrote nothing.
        assert _counts(conn) == before

    def test_refusal_isolation_foreign_company(self, conn, env):
        """Pinned refusal for list-packing-slips: no error-return path exists
        (every filter is optional), so the refusal is cross-scope isolation
        — a foreign company sees zero rows, DB unchanged."""
        dn_id, dni_id = self._dn_with_item(conn, env)
        assert is_ok(call_action(mod.add_packing_slip, conn, ns(
            delivery_note_id=dn_id,
            items=json.dumps([{"delivery_note_item_id": dni_id,
                               "qty_packed": "4"}]),
            posting_date="2026-06-15", notes=None, reason=None,
            company_id=env["company_id"],
        )))
        other_company = seed_company(conn)
        seed_customer(conn, other_company, "Foreign Customer")
        before = _counts(conn)
        result = call_action(mod.list_packing_slips, conn, ns(
            delivery_note_id=None, company_id=other_company,
            limit="20", offset="0",
        ))
        assert is_ok(result)
        assert result["count"] == 0
        assert result["packing_slips"] == []
        assert _counts(conn) == before
