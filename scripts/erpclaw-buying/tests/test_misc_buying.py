"""Tests for erpclaw-buying material requests, RFQs, and misc actions.

Actions tested:
  - add-material-request, submit-material-request, list-material-requests
  - add-rfq, submit-rfq, list-rfqs
  - add-supplier-quotation, list-supplier-quotations
  - compare-supplier-quotations
  - add-landed-cost-voucher
  - status
"""
import json
import pytest
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_supplier,
)

mod = load_db_query()


def _items(env, *specs):
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r, "warehouse_id": env["warehouse"]}
        for k, q, r in specs
    ])


# ──────────────────────────────────────────────────────────────────────────────
# Material Requests
# ──────────────────────────────────────────────────────────────────────────────

class TestAddMaterialRequest:
    def test_basic_create(self, conn, env):
        items = _items(env, ("item1", "20", "0"))
        result = call_action(mod.add_material_request, conn, ns(
            request_type="purchase", items=items,
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "material_request_id" in result

    def test_missing_items_fails(self, conn, env):
        result = call_action(mod.add_material_request, conn, ns(
            request_type="purchase", items=None,
            company_id=env["company_id"],
        ))
        assert is_error(result)


class TestSubmitMaterialRequest:
    def test_submit(self, conn, env):
        items = _items(env, ("item1", "10", "0"))
        create = call_action(mod.add_material_request, conn, ns(
            request_type="purchase", items=items,
            company_id=env["company_id"],
        ))
        result = call_action(mod.submit_material_request, conn, ns(
            material_request_id=create["material_request_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM material_request WHERE id=?",
                           (create["material_request_id"],)).fetchone()
        assert row["status"] in ("submitted", "open", "pending")


class TestListMaterialRequests:
    def test_list(self, conn, env):
        items = _items(env, ("item1", "5", "0"))
        call_action(mod.add_material_request, conn, ns(
            request_type="purchase", items=items,
            company_id=env["company_id"],
        ))
        result = call_action(mod.list_material_requests, conn, ns(
            company_id=env["company_id"], search=None,
            from_date=None, to_date=None, mr_status=None,
            request_type=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# RFQs
# ──────────────────────────────────────────────────────────────────────────────

class TestAddRFQ:
    def test_basic_create(self, conn, env):
        items = _items(env, ("item1", "50", "0"))
        suppliers = json.dumps([env["supplier"]])
        result = call_action(mod.add_rfq, conn, ns(
            items=items, suppliers=suppliers,
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "rfq_id" in result

    def test_missing_suppliers_fails(self, conn, env):
        items = _items(env, ("item1", "10", "0"))
        result = call_action(mod.add_rfq, conn, ns(
            items=items, suppliers=None,
            company_id=env["company_id"],
        ))
        assert is_error(result)


class TestListRFQs:
    def test_list(self, conn, env):
        items = _items(env, ("item1", "10", "0"))
        suppliers = json.dumps([env["supplier"]])
        call_action(mod.add_rfq, conn, ns(
            items=items, suppliers=suppliers,
            company_id=env["company_id"],
        ))
        result = call_action(mod.list_rfqs, conn, ns(
            company_id=env["company_id"], search=None,
            rfq_status=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Supplier Quotations
# ──────────────────────────────────────────────────────────────────────────────

class TestAddSupplierQuotation:
    def test_basic_create(self, conn, env):
        # First create an RFQ
        items = _items(env, ("item1", "50", "0"))
        suppliers = json.dumps([env["supplier"]])
        rfq = call_action(mod.add_rfq, conn, ns(
            items=items, suppliers=suppliers,
            company_id=env["company_id"],
        ))
        # Get rfq_item_id from the database
        rfq_item = conn.execute(
            "SELECT id FROM rfq_item WHERE rfq_id=?",
            (rfq["rfq_id"],)
        ).fetchone()
        sq_items = json.dumps([
            {"rfq_item_id": rfq_item["id"], "rate": "45.00"}
        ])
        result = call_action(mod.add_supplier_quotation, conn, ns(
            rfq_id=rfq["rfq_id"], supplier_id=env["supplier"],
            items=sq_items, company_id=env["company_id"],
            tax_template_id=None,
        ))
        assert is_ok(result)
        assert "supplier_quotation_id" in result


class TestListSupplierQuotations:
    def test_list(self, conn, env):
        items = _items(env, ("item1", "10", "0"))
        suppliers = json.dumps([env["supplier"]])
        rfq = call_action(mod.add_rfq, conn, ns(
            items=items, suppliers=suppliers,
            company_id=env["company_id"],
        ))
        result = call_action(mod.list_supplier_quotations, conn, ns(
            rfq_id=rfq["rfq_id"], company_id=env["company_id"],
            supplier_id=None, sq_status=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)


# ──────────────────────────────────────────────────────────────────────────────
# Status
# ──────────────────────────────────────────────────────────────────────────────

class TestStatus:
    def test_status(self, conn, env):
        result = call_action(mod.status_action, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
