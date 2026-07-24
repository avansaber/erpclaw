"""Tests for erpclaw-buying MR -> PO consumption (WS2/D2, dossier §2).

Actions tested: create-po-from-material-request, get-material-request
Covers: full-copy correctness (exact Decimals), partial-then-complete ordering
with status transitions submitted -> partially_ordered -> ordered, over-order
refused, draft MR refused, get-material-request parent+items shape.
"""
import json
import pytest
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _mr_ns(**kw):
    defaults = dict(
        material_request_id=None, request_type="purchase", items=None,
        company_id=None, supplier_id=None, posting_date=None,
        tax_template_id=None, mr_status=None, limit=None, offset=None,
    )
    defaults.update(kw)
    return ns(**defaults)


def _add_mr(conn, env, items):
    """Create a draft MR. items = [(item_key, qty, warehouse_or_None), ...]"""
    payload = json.dumps([
        {"item_id": env[k], "qty": q,
         **({"warehouse_id": env["warehouse"]} if wh else {})}
        for k, q, wh in items
    ])
    res = call_action(mod.add_material_request, conn, _mr_ns(
        items=payload, company_id=env["company_id"]))
    assert is_ok(res), res
    return res["material_request_id"]


def _submit_mr(conn, mr_id):
    res = call_action(mod.submit_material_request, conn,
                      _mr_ns(material_request_id=mr_id))
    assert is_ok(res), res
    return res


def _set_item_rates(conn, item_id, last_purchase_rate=None, standard_rate=None):
    if last_purchase_rate is not None:
        conn.execute("UPDATE item SET last_purchase_rate = ? WHERE id = ?",
                     (last_purchase_rate, item_id))
    if standard_rate is not None:
        conn.execute("UPDATE item SET standard_rate = ? WHERE id = ?",
                     (standard_rate, item_id))
    conn.commit()


def _create_po_from_mr(conn, env, mr_id, overrides=None, supplier=None):
    return call_action(mod.create_po_from_material_request, conn, _mr_ns(
        material_request_id=mr_id,
        supplier_id=supplier or env["supplier"],
        items=json.dumps(overrides) if overrides is not None else None,
        posting_date="2026-07-22",
    ))


def _mr_row(conn, mr_id):
    return conn.execute(
        "SELECT * FROM material_request WHERE id = ?", (mr_id,)).fetchone()


def _mr_items(conn, mr_id):
    return conn.execute(
        """SELECT * FROM material_request_item
           WHERE material_request_id = ? ORDER BY rowid""", (mr_id,)).fetchall()


def _po_items(conn, po_id):
    return conn.execute(
        """SELECT * FROM purchase_order_item
           WHERE purchase_order_id = ? ORDER BY rowid""", (po_id,)).fetchall()


class TestFullCopy:
    def test_full_mr_to_po_copy_correctness(self, conn, env):
        """Full conversion copies item/qty/uom/warehouse, resolves rates from
        the item master, and lands exact Decimal totals."""
        _set_item_rates(conn, env["item1"], last_purchase_rate="50.00")
        _set_item_rates(conn, env["item2"], standard_rate="100.00")
        mr_id = _add_mr(conn, env, [("item1", "10", True), ("item2", "4", True)])
        _submit_mr(conn, mr_id)

        res = _create_po_from_mr(conn, env, mr_id)
        assert is_ok(res), res
        assert res["material_request_id"] == mr_id
        assert res["items_ordered"] == 2
        # 10 x 50.00 + 4 x 100.00 = 900.00, no tax
        assert Decimal(res["total_amount"]) == Decimal("900.00")
        assert Decimal(res["tax_amount"]) == Decimal("0.00")
        assert Decimal(res["grand_total"]) == Decimal("900.00")
        assert res["material_request_status"] == "ordered"

        po = conn.execute("SELECT * FROM purchase_order WHERE id = ?",
                          (res["purchase_order_id"],)).fetchone()
        assert po["status"] == "draft"
        assert po["supplier_id"] == env["supplier"]
        assert po["company_id"] == env["company_id"]
        assert Decimal(po["grand_total"]) == Decimal("900.00")

        rows = _po_items(conn, res["purchase_order_id"])
        assert len(rows) == 2
        by_item = {r["item_id"]: r for r in rows}
        r1 = by_item[env["item1"]]
        assert Decimal(r1["quantity"]) == Decimal("10.00")
        assert Decimal(r1["rate"]) == Decimal("50.00")
        assert Decimal(r1["amount"]) == Decimal("500.00")
        assert Decimal(r1["net_amount"]) == Decimal("500.00")
        assert r1["warehouse_id"] == env["warehouse"]
        assert r1["uom"] == "Each"          # MR line uom NULL -> item stock_uom
        r2 = by_item[env["item2"]]
        assert Decimal(r2["quantity"]) == Decimal("4.00")
        assert Decimal(r2["rate"]) == Decimal("100.00")   # standard_rate fallback
        assert Decimal(r2["net_amount"]) == Decimal("400.00")

        # MR lines fully consumed
        for line in _mr_items(conn, mr_id):
            assert Decimal(line["ordered_qty"]) == Decimal(line["quantity"])
        assert _mr_row(conn, mr_id)["status"] == "ordered"

    def test_explicit_rate_override_beats_item_master(self, conn, env):
        _set_item_rates(conn, env["item1"], last_purchase_rate="50.00")
        mr_id = _add_mr(conn, env, [("item1", "3", True)])
        _submit_mr(conn, mr_id)

        res = _create_po_from_mr(conn, env, mr_id, overrides=[
            {"item_id": env["item1"], "rate": "72.50"}])
        assert is_ok(res), res
        assert Decimal(res["total_amount"]) == Decimal("217.50")
        row = _po_items(conn, res["purchase_order_id"])[0]
        assert Decimal(row["rate"]) == Decimal("72.50")

    def test_no_resolvable_rate_refused(self, conn, env):
        """Mirrors add-purchase-order's rate>0 contract: no override and a
        zero-rate item master -> refuse, nothing written."""
        mr_id = _add_mr(conn, env, [("item1", "5", True)])
        _submit_mr(conn, mr_id)

        res = _create_po_from_mr(conn, env, mr_id)
        assert is_error(res)
        assert "rate" in res["message"]
        assert _mr_row(conn, mr_id)["status"] == "submitted"
        assert Decimal(_mr_items(conn, mr_id)[0]["ordered_qty"]) == Decimal("0")
        assert conn.execute("SELECT COUNT(*) FROM purchase_order").fetchone()[0] == 0


class TestPartialOrdering:
    def test_partial_then_complete_status_transitions(self, conn, env):
        """draft/submitted -> partially_ordered -> ordered; a second call
        orders exactly the remainder."""
        _set_item_rates(conn, env["item1"], last_purchase_rate="10.00")
        _set_item_rates(conn, env["item2"], last_purchase_rate="20.00")
        mr_id = _add_mr(conn, env, [("item1", "10", True), ("item2", "6", True)])
        assert _mr_row(conn, mr_id)["status"] == "draft"
        _submit_mr(conn, mr_id)
        assert _mr_row(conn, mr_id)["status"] == "submitted"

        # Call 1: 4 of 10 on item1, skip item2 entirely (qty 0)
        res1 = _create_po_from_mr(conn, env, mr_id, overrides=[
            {"item_id": env["item1"], "qty": "4"},
            {"item_id": env["item2"], "qty": "0"},
        ])
        assert is_ok(res1), res1
        assert res1["items_ordered"] == 1
        assert Decimal(res1["total_amount"]) == Decimal("40.00")
        assert res1["material_request_status"] == "partially_ordered"
        assert _mr_row(conn, mr_id)["status"] == "partially_ordered"
        lines = {r["item_id"]: r for r in _mr_items(conn, mr_id)}
        assert Decimal(lines[env["item1"]]["ordered_qty"]) == Decimal("4.00")
        assert Decimal(lines[env["item2"]]["ordered_qty"]) == Decimal("0")

        # Call 2: no overrides -> remainder (6 of item1, 6 of item2)
        res2 = _create_po_from_mr(conn, env, mr_id)
        assert is_ok(res2), res2
        assert res2["items_ordered"] == 2
        # 6 x 10.00 + 6 x 20.00 = 180.00
        assert Decimal(res2["total_amount"]) == Decimal("180.00")
        assert res2["material_request_status"] == "ordered"
        assert _mr_row(conn, mr_id)["status"] == "ordered"
        lines = {r["item_id"]: r for r in _mr_items(conn, mr_id)}
        assert Decimal(lines[env["item1"]]["ordered_qty"]) == Decimal("10.00")
        assert Decimal(lines[env["item2"]]["ordered_qty"]) == Decimal("6.00")
        assert res2["purchase_order_id"] != res1["purchase_order_id"]

    def test_fully_ordered_mr_refused(self, conn, env):
        _set_item_rates(conn, env["item1"], last_purchase_rate="10.00")
        mr_id = _add_mr(conn, env, [("item1", "2", True)])
        _submit_mr(conn, mr_id)
        assert is_ok(_create_po_from_mr(conn, env, mr_id))

        res = _create_po_from_mr(conn, env, mr_id)
        assert is_error(res)
        assert "ordered" in res["message"]


class TestRefusals:
    def test_over_order_refused(self, conn, env):
        """Ordering more than the remaining unordered quantity is refused and
        nothing is written (single-transaction rollback)."""
        _set_item_rates(conn, env["item1"], last_purchase_rate="10.00")
        mr_id = _add_mr(conn, env, [("item1", "10", True)])
        _submit_mr(conn, mr_id)

        res = _create_po_from_mr(conn, env, mr_id, overrides=[
            {"item_id": env["item1"], "qty": "11"}])
        assert is_error(res)
        assert "exceed" in res["message"]
        assert Decimal(_mr_items(conn, mr_id)[0]["ordered_qty"]) == Decimal("0")
        assert conn.execute("SELECT COUNT(*) FROM purchase_order").fetchone()[0] == 0

        # Partial first, then over-order the remainder
        assert is_ok(_create_po_from_mr(conn, env, mr_id, overrides=[
            {"item_id": env["item1"], "qty": "7"}]))
        res = _create_po_from_mr(conn, env, mr_id, overrides=[
            {"item_id": env["item1"], "qty": "4"}])
        assert is_error(res)
        assert "exceed" in res["message"]
        assert Decimal(_mr_items(conn, mr_id)[0]["ordered_qty"]) == Decimal("7.00")

    def test_draft_mr_refused(self, conn, env):
        _set_item_rates(conn, env["item1"], last_purchase_rate="10.00")
        mr_id = _add_mr(conn, env, [("item1", "5", True)])

        res = _create_po_from_mr(conn, env, mr_id)
        assert is_error(res)
        assert "draft" in res["message"]
        assert _mr_row(conn, mr_id)["status"] == "draft"

    def test_non_purchase_request_type_refused(self, conn, env):
        mr_id = _add_mr(conn, env, [("item1", "5", True)])
        conn.execute(
            "UPDATE material_request SET request_type = 'material_transfer', "
            "status = 'submitted' WHERE id = ?", (mr_id,))
        conn.commit()
        res = _create_po_from_mr(conn, env, mr_id)
        assert is_error(res)
        assert "purchase" in res["message"]

    def test_missing_args_and_unknown_ids_refused(self, conn, env):
        assert is_error(_create_po_from_mr(conn, env, None))
        assert is_error(_create_po_from_mr(conn, env, "nope"))
        mr_id = _add_mr(conn, env, [("item1", "5", True)])
        _submit_mr(conn, mr_id)
        assert is_error(_create_po_from_mr(conn, env, mr_id, supplier="ghost"))

    def test_inactive_supplier_refused(self, conn, env):
        _set_item_rates(conn, env["item1"], last_purchase_rate="10.00")
        mr_id = _add_mr(conn, env, [("item1", "5", True)])
        _submit_mr(conn, mr_id)
        conn.execute("UPDATE supplier SET status = 'blocked' WHERE id = ?",
                     (env["supplier"],))
        conn.commit()
        res = _create_po_from_mr(conn, env, mr_id)
        assert is_error(res)
        assert "blocked" in res["message"]

    def test_override_for_item_not_on_mr_refused(self, conn, env):
        _set_item_rates(conn, env["item1"], last_purchase_rate="10.00")
        mr_id = _add_mr(conn, env, [("item1", "5", True)])
        _submit_mr(conn, mr_id)
        res = _create_po_from_mr(conn, env, mr_id, overrides=[
            {"item_id": env["item2"], "qty": "1"}])
        assert is_error(res)
        assert "not on this material request" in res["message"]


class TestGetMaterialRequest:
    def test_get_returns_parent_and_items(self, conn, env):
        mr_id = _add_mr(conn, env, [("item1", "10", True), ("item2", "6", False)])
        res = call_action(mod.get_material_request, conn,
                          _mr_ns(material_request_id=mr_id))
        assert is_ok(res), res
        assert res["id"] == mr_id
        # NOTE sibling get-* shape: the ok() envelope owns the top-level
        # "status" key (== "ok"), masking the document status — same as
        # get-purchase-order. Document status asserted from the DB row.
        assert _mr_row(conn, mr_id)["status"] == "draft"
        assert res["request_type"] == "purchase"
        assert len(res["items"]) == 2
        by_item = {r["item_id"]: r for r in res["items"]}
        line1 = by_item[env["item1"]]
        assert Decimal(line1["quantity"]) == Decimal("10.00")
        assert Decimal(line1["ordered_qty"]) == Decimal("0")
        assert line1["warehouse_id"] == env["warehouse"]
        assert line1["item_code"]           # joined from item, sibling get-* shape
        assert line1["item_name"]

    def test_get_reflects_ordered_qty_after_conversion(self, conn, env):
        _set_item_rates(conn, env["item1"], last_purchase_rate="10.00")
        mr_id = _add_mr(conn, env, [("item1", "10", True)])
        _submit_mr(conn, mr_id)
        assert is_ok(_create_po_from_mr(conn, env, mr_id, overrides=[
            {"item_id": env["item1"], "qty": "4"}]))

        res = call_action(mod.get_material_request, conn,
                          _mr_ns(material_request_id=mr_id))
        assert is_ok(res), res
        assert _mr_row(conn, mr_id)["status"] == "partially_ordered"
        assert Decimal(res["items"][0]["ordered_qty"]) == Decimal("4.00")

    def test_get_missing_or_unknown_refused(self, conn, env):
        assert is_error(call_action(mod.get_material_request, conn,
                                    _mr_ns(material_request_id=None)))
        assert is_error(call_action(mod.get_material_request, conn,
                                    _mr_ns(material_request_id="nope")))
