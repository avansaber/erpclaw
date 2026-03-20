"""Tests for blanket purchase order actions.

Actions tested:
  - add-blanket-po
  - submit-blanket-po
  - get-blanket-po
  - list-blanket-pos
  - create-po-from-blanket
"""
import json
import pytest
from decimal import Decimal
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r}
        for k, q, r in specs
    ])


def _create_blanket(conn, env, items_str=None, **overrides):
    """Create a draft blanket PO."""
    items_str = items_str or _items(env, ("item1", "100", "50.00"))
    defaults = dict(
        supplier_id=env["supplier"], company_id=env["company_id"],
        items=items_str, valid_from="2026-01-01", valid_to="2027-12-31",
        blanket_order_id=None, blanket_status=None,
        tax_template_id=None, posting_date=None,
        name=None, sales_order_id=None,
        template_id=None, frequency=None, start_date=None,
        end_date=None, as_of_date=None, auto_submit=False,
        template_status=None,
    )
    defaults.update(overrides)
    return call_action(mod.add_blanket_po, conn, ns(**defaults))


def _submit_blanket(conn, env, blanket_order_id):
    """Submit a blanket PO."""
    return call_action(mod.submit_blanket_po, conn, ns(
        blanket_order_id=blanket_order_id,
    ))


# --------------------------------------------------------------------------
# add-blanket-po
# --------------------------------------------------------------------------

class TestAddBlanketPO:
    def test_basic_create(self, conn, env):
        result = _create_blanket(conn, env)
        assert is_ok(result)
        assert "blanket_order_id" in result
        assert Decimal(result["total_qty"]) == Decimal("100")

    def test_multi_item(self, conn, env):
        items = _items(env, ("item1", "100", "50.00"), ("item2", "50", "100.00"))
        result = _create_blanket(conn, env, items_str=items)
        assert is_ok(result)
        assert Decimal(result["total_qty"]) == Decimal("150")

    def test_missing_supplier_fails(self, conn, env):
        result = call_action(mod.add_blanket_po, conn, ns(
            supplier_id=None, company_id=env["company_id"],
            items=_items(env, ("item1", "10", "50.00")),
            valid_from="2026-01-01", valid_to="2027-12-31",
            blanket_order_id=None, blanket_status=None,
            tax_template_id=None, posting_date=None,
            name=None, sales_order_id=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_error(result)

    def test_invalid_dates_fails(self, conn, env):
        result = _create_blanket(conn, env,
            valid_from="2027-01-01", valid_to="2026-01-01")
        assert is_error(result)

    def test_missing_items_fails(self, conn, env):
        result = call_action(mod.add_blanket_po, conn, ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            items=None, valid_from="2026-01-01", valid_to="2027-12-31",
            blanket_order_id=None, blanket_status=None,
            tax_template_id=None, posting_date=None,
            name=None, sales_order_id=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_error(result)


# --------------------------------------------------------------------------
# submit-blanket-po
# --------------------------------------------------------------------------

class TestSubmitBlanketPO:
    def test_submit(self, conn, env):
        bo = _create_blanket(conn, env)
        result = _submit_blanket(conn, env, bo["blanket_order_id"])
        assert is_ok(result)
        assert result["doc_status"] == "active"

    def test_submit_already_active_fails(self, conn, env):
        bo = _create_blanket(conn, env)
        _submit_blanket(conn, env, bo["blanket_order_id"])
        result = _submit_blanket(conn, env, bo["blanket_order_id"])
        assert is_error(result)


# --------------------------------------------------------------------------
# get-blanket-po
# --------------------------------------------------------------------------

class TestGetBlanketPO:
    def test_get_with_items(self, conn, env):
        items = _items(env, ("item1", "100", "50.00"), ("item2", "50", "100.00"))
        bo = _create_blanket(conn, env, items_str=items)
        result = call_action(mod.get_blanket_po, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
        ))
        assert is_ok(result)
        assert result["blanket_order_type"] == "buying"
        assert len(result["items"]) == 2


# --------------------------------------------------------------------------
# list-blanket-pos
# --------------------------------------------------------------------------

class TestListBlanketPOs:
    def test_list(self, conn, env):
        _create_blanket(conn, env)
        result = call_action(mod.list_blanket_pos, conn, ns(
            company_id=env["company_id"], supplier_id=None,
            blanket_status=None, limit="20", offset="0",
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1
        assert all(bo["blanket_order_type"] == "buying"
                   for bo in result["blanket_orders"])


# --------------------------------------------------------------------------
# create-po-from-blanket
# --------------------------------------------------------------------------

class TestCreatePOFromBlanket:
    def test_create_po(self, conn, env):
        bo = _create_blanket(conn, env)
        _submit_blanket(conn, env, bo["blanket_order_id"])

        po_items = json.dumps([{"item_id": env["item1"], "qty": "30"}])
        result = call_action(mod.create_po_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=po_items, posting_date="2026-06-01",
            tax_template_id=None, company_id=env["company_id"],
            supplier_id=None, name=None, sales_order_id=None,
            blanket_status=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_ok(result)
        assert "purchase_order_id" in result
        assert Decimal(result["total_amount"]) == Decimal("1500.00")

    def test_exceeds_blanket_qty_fails(self, conn, env):
        bo = _create_blanket(conn, env)
        _submit_blanket(conn, env, bo["blanket_order_id"])

        po_items = json.dumps([{"item_id": env["item1"], "qty": "200"}])
        result = call_action(mod.create_po_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=po_items, posting_date="2026-06-01",
            tax_template_id=None, company_id=env["company_id"],
            supplier_id=None, name=None, sales_order_id=None,
            blanket_status=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_error(result)

    def test_cumulative_drawdown(self, conn, env):
        """Multiple POs accumulate ordered_qty on blanket."""
        bo = _create_blanket(conn, env)
        _submit_blanket(conn, env, bo["blanket_order_id"])

        common = dict(
            blanket_order_id=bo["blanket_order_id"],
            tax_template_id=None, company_id=env["company_id"],
            supplier_id=None, name=None, sales_order_id=None,
            blanket_status=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        )

        # First PO: 40 of 100
        r1 = call_action(mod.create_po_from_blanket, conn, ns(
            items=json.dumps([{"item_id": env["item1"], "qty": "40"}]),
            posting_date="2026-06-01", **common,
        ))
        assert is_ok(r1)

        # Second PO: 50 of remaining 60
        r2 = call_action(mod.create_po_from_blanket, conn, ns(
            items=json.dumps([{"item_id": env["item1"], "qty": "50"}]),
            posting_date="2026-06-02", **common,
        ))
        assert is_ok(r2)

        # Third PO: 20 of remaining 10 -> fails
        r3 = call_action(mod.create_po_from_blanket, conn, ns(
            items=json.dumps([{"item_id": env["item1"], "qty": "20"}]),
            posting_date="2026-06-03", **common,
        ))
        assert is_error(r3)

    def test_expired_blanket_fails(self, conn, env):
        bo = _create_blanket(conn, env, valid_from="2024-01-01", valid_to="2024-12-31")
        _submit_blanket(conn, env, bo["blanket_order_id"])

        po_items = json.dumps([{"item_id": env["item1"], "qty": "10"}])
        result = call_action(mod.create_po_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=po_items, posting_date="2026-06-01",
            tax_template_id=None, company_id=env["company_id"],
            supplier_id=None, name=None, sales_order_id=None,
            blanket_status=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_error(result)

    def test_from_draft_blanket_fails(self, conn, env):
        bo = _create_blanket(conn, env)
        po_items = json.dumps([{"item_id": env["item1"], "qty": "10"}])
        result = call_action(mod.create_po_from_blanket, conn, ns(
            blanket_order_id=bo["blanket_order_id"],
            items=po_items, posting_date="2026-06-01",
            tax_template_id=None, company_id=env["company_id"],
            supplier_id=None, name=None, sales_order_id=None,
            blanket_status=None,
            template_id=None, frequency=None, start_date=None,
            end_date=None, as_of_date=None, auto_submit=False,
            template_status=None,
        ))
        assert is_error(result)
