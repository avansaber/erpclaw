"""Tests for RFQ and supplier quotation actions:
add-rfq, submit-rfq, list-rfqs, add-supplier-quotation,
list-supplier-quotations, compare-supplier-quotations.
"""
import json

import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_item,
    create_test_naming_series,
    create_test_supplier,
    setup_buying_environment,
)
from db_query import ACTIONS


class TestAddRfq:
    """Tests for the add-rfq action."""

    def test_add_rfq(self, fresh_db):
        """Create an RFQ with items and suppliers, verify success."""
        company_id = create_test_company(fresh_db)
        item_id = create_test_item(fresh_db)
        supplier_id = create_test_supplier(fresh_db, company_id)

        items_json = json.dumps([{"item_id": item_id, "qty": "10"}])
        suppliers_json = json.dumps([supplier_id])

        result = _call_action(
            ACTIONS["add-rfq"], fresh_db,
            items=items_json,
            suppliers=suppliers_json,
            company_id=company_id,
        )
        assert result["status"] == "ok"
        assert "rfq_id" in result
        assert result["item_count"] == 1
        assert result["supplier_count"] == 1

        # Verify persisted in database
        rfq = fresh_db.execute(
            "SELECT * FROM request_for_quotation WHERE id = ?",
            (result["rfq_id"],),
        ).fetchone()
        assert rfq is not None
        assert rfq["status"] == "draft"
        assert rfq["company_id"] == company_id

        # Verify RFQ items
        rfq_items = fresh_db.execute(
            "SELECT * FROM rfq_item WHERE rfq_id = ?",
            (result["rfq_id"],),
        ).fetchall()
        assert len(rfq_items) == 1
        assert rfq_items[0]["item_id"] == item_id
        assert rfq_items[0]["quantity"] == "10.00"

        # Verify RFQ suppliers
        rfq_suppliers = fresh_db.execute(
            "SELECT * FROM rfq_supplier WHERE rfq_id = ?",
            (result["rfq_id"],),
        ).fetchall()
        assert len(rfq_suppliers) == 1
        assert rfq_suppliers[0]["supplier_id"] == supplier_id


class TestSubmitRfq:
    """Tests for the submit-rfq action."""

    def test_submit_rfq(self, fresh_db):
        """Submit an RFQ and verify status becomes submitted."""
        company_id = create_test_company(fresh_db)
        create_test_naming_series(fresh_db, company_id)
        item_id = create_test_item(fresh_db)
        supplier_id = create_test_supplier(fresh_db, company_id)

        items_json = json.dumps([{"item_id": item_id, "qty": "10"}])
        suppliers_json = json.dumps([supplier_id])

        add_result = _call_action(
            ACTIONS["add-rfq"], fresh_db,
            items=items_json,
            suppliers=suppliers_json,
            company_id=company_id,
        )
        assert add_result["status"] == "ok"
        rfq_id = add_result["rfq_id"]

        # Submit
        result = _call_action(
            ACTIONS["submit-rfq"], fresh_db,
            rfq_id=rfq_id,
        )
        assert result["status"] == "ok"
        assert result["rfq_id"] == rfq_id
        assert result["status"] == "ok"
        assert "naming_series" in result

        # Verify in database
        rfq = fresh_db.execute(
            "SELECT * FROM request_for_quotation WHERE id = ?", (rfq_id,)
        ).fetchone()
        assert rfq["status"] == "submitted"
        assert rfq["naming_series"] is not None


class TestListRfqs:
    """Tests for the list-rfqs action."""

    def test_list_rfqs(self, fresh_db):
        """Create 2 RFQs and verify list returns both."""
        company_id = create_test_company(fresh_db)
        item_id = create_test_item(fresh_db)
        supplier_id = create_test_supplier(fresh_db, company_id)

        items_json = json.dumps([{"item_id": item_id, "qty": "10"}])
        suppliers_json = json.dumps([supplier_id])

        r1 = _call_action(
            ACTIONS["add-rfq"], fresh_db,
            items=items_json,
            suppliers=suppliers_json,
            company_id=company_id,
        )
        assert r1["status"] == "ok"

        r2 = _call_action(
            ACTIONS["add-rfq"], fresh_db,
            items=items_json,
            suppliers=suppliers_json,
            company_id=company_id,
        )
        assert r2["status"] == "ok"

        result = _call_action(
            ACTIONS["list-rfqs"], fresh_db,
            company_id=company_id,
        )
        assert result["status"] == "ok"
        assert result["total_count"] == 2
        assert len(result["rfqs"]) == 2


class TestAddSupplierQuotation:
    """Tests for the add-supplier-quotation action."""

    def test_add_supplier_quotation(self, fresh_db):
        """Create a supplier quotation for a submitted RFQ."""
        company_id = create_test_company(fresh_db)
        create_test_naming_series(fresh_db, company_id)
        item_id = create_test_item(fresh_db)
        supplier_id = create_test_supplier(fresh_db, company_id, name="Quoting Supplier")

        items_json = json.dumps([{"item_id": item_id, "qty": "10"}])
        suppliers_json = json.dumps([supplier_id])

        # Create and submit RFQ
        add_result = _call_action(
            ACTIONS["add-rfq"], fresh_db,
            items=items_json,
            suppliers=suppliers_json,
            company_id=company_id,
        )
        assert add_result["status"] == "ok"
        rfq_id = add_result["rfq_id"]

        submit_result = _call_action(
            ACTIONS["submit-rfq"], fresh_db,
            rfq_id=rfq_id,
        )
        assert submit_result["status"] == "ok"

        # Get the rfq_item_id for referencing in the supplier quotation
        rfq_item = fresh_db.execute(
            "SELECT id FROM rfq_item WHERE rfq_id = ?", (rfq_id,)
        ).fetchone()
        rfq_item_id = rfq_item["id"]

        # Create supplier quotation
        sq_items_json = json.dumps([
            {"rfq_item_id": rfq_item_id, "rate": "25.00"}
        ])
        result = _call_action(
            ACTIONS["add-supplier-quotation"], fresh_db,
            rfq_id=rfq_id,
            supplier_id=supplier_id,
            items=sq_items_json,
        )
        assert result["status"] == "ok"
        assert "supplier_quotation_id" in result
        assert result["total_amount"] == "250.00"  # 10 qty * 25.00 rate

        # Verify persisted
        sq = fresh_db.execute(
            "SELECT * FROM supplier_quotation WHERE id = ?",
            (result["supplier_quotation_id"],),
        ).fetchone()
        assert sq is not None
        assert sq["supplier_id"] == supplier_id
        assert sq["rfq_id"] == rfq_id
        assert sq["total_amount"] == "250.00"
        assert sq["grand_total"] == "250.00"


class TestListSupplierQuotations:
    """Tests for the list-supplier-quotations action."""

    def test_list_supplier_quotations(self, fresh_db):
        """Create 2 supplier quotations for an RFQ and verify list returns both."""
        company_id = create_test_company(fresh_db)
        create_test_naming_series(fresh_db, company_id)
        item_id = create_test_item(fresh_db)
        supplier_a = create_test_supplier(fresh_db, company_id, name="Supplier A")
        supplier_b = create_test_supplier(fresh_db, company_id, name="Supplier B")

        items_json = json.dumps([{"item_id": item_id, "qty": "10"}])
        suppliers_json = json.dumps([supplier_a, supplier_b])

        # Create and submit RFQ with both suppliers
        add_result = _call_action(
            ACTIONS["add-rfq"], fresh_db,
            items=items_json,
            suppliers=suppliers_json,
            company_id=company_id,
        )
        assert add_result["status"] == "ok"
        rfq_id = add_result["rfq_id"]

        submit_result = _call_action(
            ACTIONS["submit-rfq"], fresh_db,
            rfq_id=rfq_id,
        )
        assert submit_result["status"] == "ok"

        rfq_item = fresh_db.execute(
            "SELECT id FROM rfq_item WHERE rfq_id = ?", (rfq_id,)
        ).fetchone()
        rfq_item_id = rfq_item["id"]

        # Supplier A quotes 25.00
        sq_items_a = json.dumps([{"rfq_item_id": rfq_item_id, "rate": "25.00"}])
        r1 = _call_action(
            ACTIONS["add-supplier-quotation"], fresh_db,
            rfq_id=rfq_id,
            supplier_id=supplier_a,
            items=sq_items_a,
        )
        assert r1["status"] == "ok"

        # Supplier B quotes 30.00
        sq_items_b = json.dumps([{"rfq_item_id": rfq_item_id, "rate": "30.00"}])
        r2 = _call_action(
            ACTIONS["add-supplier-quotation"], fresh_db,
            rfq_id=rfq_id,
            supplier_id=supplier_b,
            items=sq_items_b,
        )
        assert r2["status"] == "ok"

        # List by RFQ
        result = _call_action(
            ACTIONS["list-supplier-quotations"], fresh_db,
            rfq_id=rfq_id,
        )
        assert result["status"] == "ok"
        assert len(result["supplier_quotations"]) == 2


class TestCompareSupplierQuotations:
    """Tests for the compare-supplier-quotations action."""

    def test_compare_supplier_quotations(self, fresh_db):
        """Create 2 SQs from different suppliers for the same RFQ, compare them."""
        company_id = create_test_company(fresh_db)
        create_test_naming_series(fresh_db, company_id)
        item_id = create_test_item(fresh_db, item_code="CMP-001", item_name="Compare Widget")
        supplier_a = create_test_supplier(fresh_db, company_id, name="Cheap Supplier")
        supplier_b = create_test_supplier(fresh_db, company_id, name="Expensive Supplier")

        items_json = json.dumps([{"item_id": item_id, "qty": "10"}])
        suppliers_json = json.dumps([supplier_a, supplier_b])

        # Create and submit RFQ
        add_result = _call_action(
            ACTIONS["add-rfq"], fresh_db,
            items=items_json,
            suppliers=suppliers_json,
            company_id=company_id,
        )
        assert add_result["status"] == "ok"
        rfq_id = add_result["rfq_id"]

        submit_result = _call_action(
            ACTIONS["submit-rfq"], fresh_db,
            rfq_id=rfq_id,
        )
        assert submit_result["status"] == "ok"

        rfq_item = fresh_db.execute(
            "SELECT id FROM rfq_item WHERE rfq_id = ?", (rfq_id,)
        ).fetchone()
        rfq_item_id = rfq_item["id"]

        # Supplier A quotes 20.00 (cheaper)
        sq_items_a = json.dumps([{"rfq_item_id": rfq_item_id, "rate": "20.00"}])
        r1 = _call_action(
            ACTIONS["add-supplier-quotation"], fresh_db,
            rfq_id=rfq_id,
            supplier_id=supplier_a,
            items=sq_items_a,
        )
        assert r1["status"] == "ok"

        # Supplier B quotes 35.00 (more expensive)
        sq_items_b = json.dumps([{"rfq_item_id": rfq_item_id, "rate": "35.00"}])
        r2 = _call_action(
            ACTIONS["add-supplier-quotation"], fresh_db,
            rfq_id=rfq_id,
            supplier_id=supplier_b,
            items=sq_items_b,
        )
        assert r2["status"] == "ok"

        # Compare
        result = _call_action(
            ACTIONS["compare-supplier-quotations"], fresh_db,
            rfq_id=rfq_id,
        )
        assert result["status"] == "ok"
        assert result["rfq_id"] == rfq_id
        assert result["supplier_count"] == 2
        assert len(result["comparison"]) == 1  # 1 item

        item_cmp = result["comparison"][0]
        assert item_cmp["item_id"] == item_id
        assert item_cmp["required_qty"] == "10.00"
        assert len(item_cmp["quotes"]) == 2
        assert item_cmp["lowest_rate"] == "20.00"
        assert item_cmp["lowest_supplier"] == "Cheap Supplier"

        # Verify is_lowest flag
        for q in item_cmp["quotes"]:
            if q["supplier_name"] == "Cheap Supplier":
                assert q["is_lowest"] is True
                assert q["rate"] == "20.00"
            elif q["supplier_name"] == "Expensive Supplier":
                assert q["is_lowest"] is False
                assert q["rate"] == "35.00"
