"""Tests for the open-order guard on stock entries (F8, stabilization WS1 D7).

A standalone material_receipt must be refused when an open purchase-order line
covers the item (the Buying flow would receive the goods AGAIN and double-count
stock); a standalone material_issue must be refused when an open sales-order
line covers the item (the delivery-note flow would subtract the stock AGAIN and
understate it). The guard fires at add-stock-entry AND submit-stock-entry,
BEFORE any write; it is same-company scoped and ignores draft/closed/cancelled
orders and lines already fully received/delivered.
"""
import json
import uuid
import pytest
from decimal import Decimal
from inventory_helpers import (
    call_action, ns, is_error, is_ok, load_db_query, seed_company,
)

mod = load_db_query()


# ── Local seed helpers (buying/selling-owned tables; direct INSERT is fine in
#    test fixtures — the guard itself only READs these tables) ──

def _uuid():
    return str(uuid.uuid4())


def seed_supplier(conn, company_id, name="Guard Supplier"):
    sid = _uuid()
    conn.execute(
        "INSERT INTO supplier (id, name, company_id) VALUES (?, ?, ?)",
        (sid, name, company_id))
    conn.commit()
    return sid


def seed_customer(conn, company_id, name="Guard Customer"):
    cid = _uuid()
    conn.execute(
        """INSERT INTO customer (id, name, company_id, customer_type, status)
           VALUES (?, ?, ?, 'company', 'active')""",
        (cid, name, company_id))
    conn.commit()
    return cid


def seed_purchase_order(conn, company_id, supplier_id, lines,
                        status="confirmed", naming_series="PO-0001"):
    """lines = [(item_id, quantity, received_qty), ...]"""
    po_id = _uuid()
    conn.execute(
        """INSERT INTO purchase_order
           (id, naming_series, supplier_id, order_date, status, company_id)
           VALUES (?, ?, ?, '2026-06-01', ?, ?)""",
        (po_id, naming_series, supplier_id, status, company_id))
    for item_id, qty, received in lines:
        conn.execute(
            """INSERT INTO purchase_order_item
               (id, purchase_order_id, item_id, quantity, received_qty, rate)
               VALUES (?, ?, ?, ?, ?, '10.00')""",
            (_uuid(), po_id, item_id, qty, received))
    conn.commit()
    return po_id


def seed_sales_order(conn, company_id, customer_id, lines,
                     status="confirmed", naming_series="SO-0001"):
    """lines = [(item_id, quantity, delivered_qty), ...]"""
    so_id = _uuid()
    conn.execute(
        """INSERT INTO sales_order
           (id, naming_series, customer_id, order_date, status, company_id)
           VALUES (?, ?, ?, '2026-06-01', ?, ?)""",
        (so_id, naming_series, customer_id, status, company_id))
    for item_id, qty, delivered in lines:
        conn.execute(
            """INSERT INTO sales_order_item
               (id, sales_order_id, item_id, quantity, delivered_qty, rate)
               VALUES (?, ?, ?, ?, ?, '25.00')""",
            (_uuid(), so_id, item_id, qty, delivered))
    conn.commit()
    return so_id


def _receipt_msg(item_id, po_id, ref):
    return (f"Cannot receive item {item_id} as a standalone stock entry: "
            f"open purchase order {ref} still has this item waiting to be "
            f"received. Receive it against the order instead "
            f"(create-purchase-receipt --purchase-order-id {po_id}, "
            f"then submit-purchase-receipt) so the order is marked "
            f"received and the stock is not counted twice.")


def _issue_msg(item_id, so_id, ref):
    return (f"Cannot issue item {item_id} as a standalone stock entry: "
            f"open sales order {ref} still has this item waiting to be "
            f"delivered. Deliver it against the order instead "
            f"(create-delivery-note --sales-order-id {so_id}, "
            f"then submit-delivery-note) so the order is marked "
            f"delivered and the stock is not subtracted twice.")


def _add_receive(conn, env, item_key="item1", qty="10", rate="50.00"):
    items = json.dumps([{"item_id": env[item_key], "qty": qty, "rate": rate,
                         "to_warehouse_id": env["warehouse"]}])
    return call_action(mod.add_stock_entry, conn, ns(
        entry_type="receive", company_id=env["company_id"],
        posting_date="2026-06-15", items=items))


def _add_issue(conn, env, item_key="item1", qty="5", rate="50.00"):
    items = json.dumps([{"item_id": env[item_key], "qty": qty, "rate": rate,
                         "from_warehouse_id": env["warehouse"]}])
    return call_action(mod.add_stock_entry, conn, ns(
        entry_type="issue", company_id=env["company_id"],
        posting_date="2026-06-15", items=items))


# ---------------------------------------------------------------------------
# material_receipt vs open purchase order
# ---------------------------------------------------------------------------

class TestReceiptGuardOpenPO:
    def test_fires_on_open_po_line(self, conn, env):
        supp = seed_supplier(conn, env["company_id"])
        po_id = seed_purchase_order(
            conn, env["company_id"], supp,
            [(env["item1"], "10", "0")], status="confirmed")
        result = _add_receive(conn, env)
        assert is_error(result)
        assert result["message"] == _receipt_msg(env["item1"], po_id, "PO-0001")
        # Nothing was written
        cnt = conn.execute("SELECT COUNT(*) AS c FROM stock_entry").fetchone()["c"]
        assert cnt == 0

    def test_fires_on_partially_received_po(self, conn, env):
        supp = seed_supplier(conn, env["company_id"])
        po_id = seed_purchase_order(
            conn, env["company_id"], supp,
            [(env["item1"], "10", "4")], status="partially_received")
        result = _add_receive(conn, env)
        assert is_error(result)
        assert result["message"] == _receipt_msg(env["item1"], po_id, "PO-0001")

    def test_no_po_allows(self, conn, env):
        result = _add_receive(conn, env)
        assert is_ok(result)
        assert Decimal(result["total_incoming_value"]) == Decimal("500.00")

    def test_fully_received_po_allows(self, conn, env):
        supp = seed_supplier(conn, env["company_id"])
        seed_purchase_order(conn, env["company_id"], supp,
                            [(env["item1"], "10", "10")],
                            status="partially_received")
        result = _add_receive(conn, env)
        assert is_ok(result)

    def test_different_item_allows(self, conn, env):
        supp = seed_supplier(conn, env["company_id"])
        seed_purchase_order(conn, env["company_id"], supp,
                            [(env["item2"], "10", "0")], status="confirmed")
        result = _add_receive(conn, env, item_key="item1")
        assert is_ok(result)

    def test_different_company_allows(self, conn, env):
        other_co = seed_company(conn, "Other Co", "OC")
        supp = seed_supplier(conn, other_co)
        seed_purchase_order(conn, other_co, supp,
                            [(env["item1"], "10", "0")], status="confirmed")
        result = _add_receive(conn, env)
        assert is_ok(result)

    def test_draft_po_allows(self, conn, env):
        supp = seed_supplier(conn, env["company_id"])
        seed_purchase_order(conn, env["company_id"], supp,
                            [(env["item1"], "10", "0")], status="draft")
        result = _add_receive(conn, env)
        assert is_ok(result)

    def test_closed_po_allows(self, conn, env):
        supp = seed_supplier(conn, env["company_id"])
        seed_purchase_order(conn, env["company_id"], supp,
                            [(env["item1"], "10", "0")], status="closed")
        result = _add_receive(conn, env)
        assert is_ok(result)

    def test_multiline_po_item_line_fully_received_allows(self, conn, env):
        # PO still open (item2's line unreceived) but item1's line is fully
        # received — receiving item1 standalone must NOT fire the guard.
        supp = seed_supplier(conn, env["company_id"])
        seed_purchase_order(
            conn, env["company_id"], supp,
            [(env["item1"], "10", "10"), (env["item2"], "5", "0")],
            status="partially_received")
        result = _add_receive(conn, env, item_key="item1")
        assert is_ok(result)

    def test_submit_side_fires_for_po_confirmed_after_draft(self, conn, env):
        # Draft created while no PO existed; PO confirmed afterwards; submit
        # must re-check and refuse BEFORE any SLE/GL write.
        draft = _add_receive(conn, env)
        assert is_ok(draft)
        supp = seed_supplier(conn, env["company_id"])
        po_id = seed_purchase_order(
            conn, env["company_id"], supp,
            [(env["item1"], "10", "0")], status="confirmed")
        result = call_action(mod.submit_stock_entry, conn, ns(
            stock_entry_id=draft["stock_entry_id"]))
        assert is_error(result)
        assert result["message"] == _receipt_msg(env["item1"], po_id, "PO-0001")
        sle_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM stock_ledger_entry WHERE voucher_id=?",
            (draft["stock_entry_id"],)).fetchone()["c"]
        assert sle_cnt == 0
        row = conn.execute("SELECT status FROM stock_entry WHERE id=?",
                           (draft["stock_entry_id"],)).fetchone()
        assert row["status"] == "draft"


# ---------------------------------------------------------------------------
# material_issue vs open sales order
# ---------------------------------------------------------------------------

class TestIssueGuardOpenSO:
    def test_fires_on_open_so_line(self, conn, env):
        cust = seed_customer(conn, env["company_id"])
        so_id = seed_sales_order(
            conn, env["company_id"], cust,
            [(env["item1"], "5", "0")], status="confirmed")
        result = _add_issue(conn, env)
        assert is_error(result)
        assert result["message"] == _issue_msg(env["item1"], so_id, "SO-0001")
        cnt = conn.execute("SELECT COUNT(*) AS c FROM stock_entry").fetchone()["c"]
        assert cnt == 0

    def test_fires_on_partially_delivered_so(self, conn, env):
        cust = seed_customer(conn, env["company_id"])
        so_id = seed_sales_order(
            conn, env["company_id"], cust,
            [(env["item1"], "5", "2")], status="partially_delivered")
        result = _add_issue(conn, env)
        assert is_error(result)
        assert result["message"] == _issue_msg(env["item1"], so_id, "SO-0001")

    def test_no_so_allows(self, conn, env):
        result = _add_issue(conn, env)
        assert is_ok(result)
        assert Decimal(result["total_outgoing_value"]) == Decimal("250.00")

    def test_fully_delivered_so_allows(self, conn, env):
        cust = seed_customer(conn, env["company_id"])
        seed_sales_order(conn, env["company_id"], cust,
                         [(env["item1"], "5", "5")],
                         status="partially_delivered")
        result = _add_issue(conn, env)
        assert is_ok(result)

    def test_different_item_allows(self, conn, env):
        cust = seed_customer(conn, env["company_id"])
        seed_sales_order(conn, env["company_id"], cust,
                         [(env["item2"], "5", "0")], status="confirmed")
        result = _add_issue(conn, env, item_key="item1")
        assert is_ok(result)

    def test_different_company_allows(self, conn, env):
        other_co = seed_company(conn, "Other Co", "OC")
        cust = seed_customer(conn, other_co)
        seed_sales_order(conn, other_co, cust,
                         [(env["item1"], "5", "0")], status="confirmed")
        result = _add_issue(conn, env)
        assert is_ok(result)

    def test_draft_so_allows(self, conn, env):
        cust = seed_customer(conn, env["company_id"])
        seed_sales_order(conn, env["company_id"], cust,
                         [(env["item1"], "5", "0")], status="draft")
        result = _add_issue(conn, env)
        assert is_ok(result)

    def test_fully_invoiced_so_allows(self, conn, env):
        # 'fully_invoiced' is outside the delivery-flow-open set that gates
        # create-delivery-note ('confirmed'/'partially_delivered'); the guard
        # must use the same definition and not fire.
        cust = seed_customer(conn, env["company_id"])
        seed_sales_order(conn, env["company_id"], cust,
                         [(env["item1"], "5", "0")], status="fully_invoiced")
        result = _add_issue(conn, env)
        assert is_ok(result)

    def test_submit_side_fires_for_so_confirmed_after_draft(self, conn, env):
        draft = _add_issue(conn, env)
        assert is_ok(draft)
        cust = seed_customer(conn, env["company_id"])
        so_id = seed_sales_order(
            conn, env["company_id"], cust,
            [(env["item1"], "5", "0")], status="confirmed")
        result = call_action(mod.submit_stock_entry, conn, ns(
            stock_entry_id=draft["stock_entry_id"]))
        assert is_error(result)
        assert result["message"] == _issue_msg(env["item1"], so_id, "SO-0001")
        sle_cnt = conn.execute(
            "SELECT COUNT(*) AS c FROM stock_ledger_entry WHERE voucher_id=?",
            (draft["stock_entry_id"],)).fetchone()["c"]
        assert sle_cnt == 0


# ---------------------------------------------------------------------------
# containment: other entry types unaffected
# ---------------------------------------------------------------------------

class TestGuardContainment:
    def test_transfer_unaffected_by_open_orders(self, conn, env):
        supp = seed_supplier(conn, env["company_id"])
        seed_purchase_order(conn, env["company_id"], supp,
                            [(env["item1"], "10", "0")], status="confirmed")
        cust = seed_customer(conn, env["company_id"])
        seed_sales_order(conn, env["company_id"], cust,
                         [(env["item1"], "5", "0")], status="confirmed")
        items = json.dumps([{"item_id": env["item1"], "qty": "5",
                             "rate": "50.00",
                             "from_warehouse_id": env["warehouse"],
                             "to_warehouse_id": env["warehouse2"]}])
        result = call_action(mod.add_stock_entry, conn, ns(
            entry_type="transfer", company_id=env["company_id"],
            posting_date="2026-06-15", items=items))
        assert is_ok(result)
