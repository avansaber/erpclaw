"""Tests for quotation actions: add-quotation, update-quotation, get-quotation,
list-quotations, submit-quotation, convert-quotation-to-so."""
import json
import pytest
from helpers import (
    _call_action,
    setup_selling_environment,
    create_test_company,
    create_test_customer,
    create_test_item,
    create_test_naming_series,
    create_test_fiscal_year,
    create_test_tax_template,
    create_test_cost_center,
    create_test_account,
)
from db_query import ACTIONS


def _make_items_json(item_id, qty="10", rate="25.00"):
    """Build the JSON string for a single-item quotation."""
    return json.dumps([{"item_id": item_id, "qty": qty, "rate": rate}])


def _create_draft_quotation(fresh_db, env, items_json=None):
    """Helper: create a draft quotation and return the result dict."""
    if items_json is None:
        items_json = _make_items_json(env["item_id"])
    result = _call_action(
        ACTIONS["add-quotation"], fresh_db,
        customer_id=env["customer_id"],
        company_id=env["company_id"],
        posting_date="2026-02-15",
        items=items_json,
    )
    assert result["status"] == "ok", f"_create_draft_quotation failed: {result}"
    return result


class TestAddQuotation:
    """Tests for the add-quotation action."""

    def test_add_quotation(self, fresh_db):
        """Create a quotation with one item and verify ok + quotation_id returned."""
        env = setup_selling_environment(fresh_db)
        items_json = _make_items_json(env["item_id"], qty="10", rate="25.00")

        result = _call_action(
            ACTIONS["add-quotation"], fresh_db,
            customer_id=env["customer_id"],
            company_id=env["company_id"],
            posting_date="2026-02-15",
            items=items_json,
        )
        assert result["status"] == "ok"
        assert "quotation_id" in result
        assert result["total_amount"] == "250.00"
        assert result["tax_amount"] == "0"
        assert result["grand_total"] == "250.00"

        # Verify the quotation row exists in the database
        row = fresh_db.execute(
            "SELECT * FROM quotation WHERE id = ?", (result["quotation_id"],)
        ).fetchone()
        assert row is not None
        assert row["status"] == "draft"
        assert row["customer_id"] == env["customer_id"]
        assert row["company_id"] == env["company_id"]

        # Verify the quotation_item child row
        items = fresh_db.execute(
            "SELECT * FROM quotation_item WHERE quotation_id = ?",
            (result["quotation_id"],),
        ).fetchall()
        assert len(items) == 1
        assert items[0]["item_id"] == env["item_id"]
        assert items[0]["quantity"] == "10.00"
        assert items[0]["rate"] == "25.00"

    def test_add_quotation_missing_customer(self, fresh_db):
        """Creating a quotation without --customer-id should return an error."""
        env = setup_selling_environment(fresh_db)
        items_json = _make_items_json(env["item_id"])

        result = _call_action(
            ACTIONS["add-quotation"], fresh_db,
            company_id=env["company_id"],
            posting_date="2026-02-15",
            items=items_json,
        )
        assert result["status"] == "error"
        assert "customer" in result["message"].lower()


class TestGetQuotation:
    """Tests for the get-quotation action."""

    def test_get_quotation(self, fresh_db):
        """Create a quotation, then get it and verify items are returned."""
        env = setup_selling_environment(fresh_db)
        items_json = _make_items_json(env["item_id"], qty="5", rate="40.00")
        add_result = _create_draft_quotation(fresh_db, env, items_json)
        quotation_id = add_result["quotation_id"]

        result = _call_action(
            ACTIONS["get-quotation"], fresh_db,
            quotation_id=quotation_id,
        )
        assert result["status"] == "ok"
        assert result["id"] == quotation_id
        assert result["customer_id"] == env["customer_id"]
        assert result["total_amount"] == "200.00"
        assert result["grand_total"] == "200.00"
        assert result["quotation_date"] == "2026-02-15"

        # Verify items sub-list
        assert "items" in result
        assert len(result["items"]) == 1
        assert result["items"][0]["item_id"] == env["item_id"]
        assert result["items"][0]["quantity"] == "5.00"
        assert result["items"][0]["rate"] == "40.00"


class TestListQuotations:
    """Tests for the list-quotations action."""

    def test_list_quotations(self, fresh_db):
        """Create two quotations and verify both appear in the list."""
        env = setup_selling_environment(fresh_db)

        # Create a second item for the second quotation
        item2_id = create_test_item(
            fresh_db, item_code="SKU-002", item_name="Widget B",
            standard_rate="30.00",
        )

        items1 = _make_items_json(env["item_id"], qty="10", rate="25.00")
        items2 = _make_items_json(item2_id, qty="5", rate="30.00")

        r1 = _create_draft_quotation(fresh_db, env, items1)
        r2 = _create_draft_quotation(fresh_db, env, items2)

        result = _call_action(
            ACTIONS["list-quotations"], fresh_db,
            company_id=env["company_id"],
        )
        assert result["status"] == "ok"
        assert result["total_count"] == 2
        assert len(result["quotations"]) == 2

        returned_ids = {q["id"] for q in result["quotations"]}
        assert r1["quotation_id"] in returned_ids
        assert r2["quotation_id"] in returned_ids


class TestUpdateQuotation:
    """Tests for the update-quotation action."""

    def test_update_quotation(self, fresh_db):
        """Update the valid_till on a draft quotation and verify updated_fields."""
        env = setup_selling_environment(fresh_db)
        add_result = _create_draft_quotation(fresh_db, env)
        quotation_id = add_result["quotation_id"]

        result = _call_action(
            ACTIONS["update-quotation"], fresh_db,
            quotation_id=quotation_id,
            valid_till="2026-03-31",
        )
        assert result["status"] == "ok"
        assert result["quotation_id"] == quotation_id
        assert "valid_until" in result["updated_fields"]

        # Verify in database
        row = fresh_db.execute(
            "SELECT valid_until FROM quotation WHERE id = ?", (quotation_id,)
        ).fetchone()
        assert row["valid_until"] == "2026-03-31"


class TestSubmitQuotation:
    """Tests for the submit-quotation action."""

    def test_submit_quotation(self, fresh_db):
        """Submit a draft quotation and verify status changes to 'open'."""
        env = setup_selling_environment(fresh_db)
        add_result = _create_draft_quotation(fresh_db, env)
        quotation_id = add_result["quotation_id"]

        result = _call_action(
            ACTIONS["submit-quotation"], fresh_db,
            quotation_id=quotation_id,
        )
        assert result["status"] == "ok"
        assert result["quotation_id"] == quotation_id
        assert result["status"] == "ok"
        assert "naming_series" in result

        # Verify in database
        row = fresh_db.execute(
            "SELECT status, naming_series FROM quotation WHERE id = ?",
            (quotation_id,),
        ).fetchone()
        assert row["status"] == "open"
        assert row["naming_series"] is not None

    def test_submit_already_submitted(self, fresh_db):
        """Submitting an already-submitted quotation should return an error."""
        env = setup_selling_environment(fresh_db)
        add_result = _create_draft_quotation(fresh_db, env)
        quotation_id = add_result["quotation_id"]

        # First submit -- should succeed
        submit1 = _call_action(
            ACTIONS["submit-quotation"], fresh_db,
            quotation_id=quotation_id,
        )
        assert submit1["status"] == "ok"

        # Second submit -- should fail because status is now 'open'
        submit2 = _call_action(
            ACTIONS["submit-quotation"], fresh_db,
            quotation_id=quotation_id,
        )
        assert submit2["status"] == "error"
        assert "draft" in submit2["message"].lower() or "cannot" in submit2["message"].lower()


class TestConvertQuotation:
    """Tests for the convert-quotation-to-so action."""

    def test_convert_quotation_to_so(self, fresh_db):
        """Submit a quotation then convert to SO; verify sales_order_id returned."""
        env = setup_selling_environment(fresh_db)
        add_result = _create_draft_quotation(fresh_db, env)
        quotation_id = add_result["quotation_id"]

        # Submit the quotation first
        submit_result = _call_action(
            ACTIONS["submit-quotation"], fresh_db,
            quotation_id=quotation_id,
        )
        assert submit_result["status"] == "ok"

        # Convert to sales order
        result = _call_action(
            ACTIONS["convert-quotation-to-so"], fresh_db,
            quotation_id=quotation_id,
        )
        assert result["status"] == "ok"
        assert result["quotation_id"] == quotation_id
        assert "sales_order_id" in result
        assert result["status"] == "ok"

        # Verify quotation status changed to 'ordered'
        q_row = fresh_db.execute(
            "SELECT status, converted_to FROM quotation WHERE id = ?",
            (quotation_id,),
        ).fetchone()
        assert q_row["status"] == "ordered"
        assert q_row["converted_to"] == result["sales_order_id"]

        # Verify the sales order was actually created
        so_row = fresh_db.execute(
            "SELECT * FROM sales_order WHERE id = ?",
            (result["sales_order_id"],),
        ).fetchone()
        assert so_row is not None
        assert so_row["customer_id"] == env["customer_id"]
        assert so_row["status"] == "draft"
        assert so_row["quotation_id"] == quotation_id

        # Verify sales order items were copied
        so_items = fresh_db.execute(
            "SELECT * FROM sales_order_item WHERE sales_order_id = ?",
            (result["sales_order_id"],),
        ).fetchall()
        assert len(so_items) == 1
        assert so_items[0]["item_id"] == env["item_id"]

    def test_convert_draft_quotation(self, fresh_db):
        """Converting an already-ordered quotation should return an error.

        Note: The code allows converting both 'draft' and 'open' quotations.
        This test verifies that an already-converted ('ordered') quotation
        cannot be converted again.
        """
        env = setup_selling_environment(fresh_db)
        add_result = _create_draft_quotation(fresh_db, env)
        quotation_id = add_result["quotation_id"]

        # Submit and convert the quotation
        _call_action(
            ACTIONS["submit-quotation"], fresh_db,
            quotation_id=quotation_id,
        )
        convert1 = _call_action(
            ACTIONS["convert-quotation-to-so"], fresh_db,
            quotation_id=quotation_id,
        )
        assert convert1["status"] == "ok"

        # Try to convert again -- should fail because status is now 'ordered'
        convert2 = _call_action(
            ACTIONS["convert-quotation-to-so"], fresh_db,
            quotation_id=quotation_id,
        )
        assert convert2["status"] == "error"
        assert "cannot" in convert2["message"].lower() or "ordered" in convert2["message"].lower()


class TestQuotationWithTax:
    """Tests for quotation with tax template applied."""

    def test_quotation_with_tax(self, fresh_db):
        """Create a quotation with a tax_template_id and verify tax is calculated."""
        env = setup_selling_environment(fresh_db)
        items_json = _make_items_json(env["item_id"], qty="10", rate="25.00")

        result = _call_action(
            ACTIONS["add-quotation"], fresh_db,
            customer_id=env["customer_id"],
            company_id=env["company_id"],
            posting_date="2026-02-15",
            items=items_json,
            tax_template_id=env["tax_template_id"],
        )
        assert result["status"] == "ok"
        assert "quotation_id" in result

        # Subtotal = 10 * 25.00 = 250.00
        assert result["total_amount"] == "250.00"
        # Tax at 8% on net total = 250.00 * 0.08 = 20.00
        assert result["tax_amount"] == "20.00"
        # Grand total = 250.00 + 20.00 = 270.00
        assert result["grand_total"] == "270.00"

        # Verify in database
        row = fresh_db.execute(
            "SELECT * FROM quotation WHERE id = ?", (result["quotation_id"],)
        ).fetchone()
        assert row["total_amount"] == "250.00"
        assert row["tax_amount"] == "20.00"
        assert row["grand_total"] == "270.00"
        assert row["tax_template_id"] == env["tax_template_id"]
