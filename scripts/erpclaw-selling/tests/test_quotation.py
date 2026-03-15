"""Tests for erpclaw-selling quotation lifecycle.

Actions tested:
  - add-quotation
  - update-quotation
  - get-quotation
  - list-quotations
  - submit-quotation
  - convert-quotation-to-so
"""
import json
import pytest
from decimal import Decimal
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r}
        for k, q, r in specs
    ])


class TestAddQuotation:
    def test_basic_create(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        result = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            valid_till="2026-07-15", tax_template_id=None,
        ))
        assert is_ok(result)
        assert "quotation_id" in result
        assert Decimal(result["total_amount"]) == Decimal("500.00")
        assert Decimal(result["grand_total"]) == Decimal("500.00")

    def test_multi_item(self, conn, env):
        items = _items(env,
            ("item1", "10", "100.00"),
            ("item2", "5", "200.00"),
        )
        result = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            valid_till=None, tax_template_id=None,
        ))
        assert is_ok(result)
        assert Decimal(result["total_amount"]) == Decimal("2000.00")

    def test_missing_customer_fails(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        result = call_action(mod.add_quotation, conn, ns(
            customer_id=None, company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            valid_till=None, tax_template_id=None,
        ))
        assert is_error(result)

    def test_missing_items_fails(self, conn, env):
        result = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=None,
            valid_till=None, tax_template_id=None,
        ))
        assert is_error(result)


class TestUpdateQuotation:
    def test_update_items(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            valid_till=None, tax_template_id=None,
        ))
        new_items = _items(env, ("item1", "10", "150.00"))
        result = call_action(mod.update_quotation, conn, ns(
            quotation_id=create["quotation_id"],
            items=new_items, valid_till=None,
        ))
        assert is_ok(result)
        assert "items" in result["updated_fields"]

    def test_update_valid_till(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        create = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            valid_till=None, tax_template_id=None,
        ))
        result = call_action(mod.update_quotation, conn, ns(
            quotation_id=create["quotation_id"],
            items=None, valid_till="2026-08-01",
        ))
        assert is_ok(result)
        assert "valid_until" in result["updated_fields"]

    def test_update_no_fields_fails(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        create = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            valid_till=None, tax_template_id=None,
        ))
        result = call_action(mod.update_quotation, conn, ns(
            quotation_id=create["quotation_id"],
            items=None, valid_till=None,
        ))
        assert is_error(result)


class TestGetQuotation:
    def test_get_with_items(self, conn, env):
        items = _items(env, ("item1", "3", "50.00"))
        create = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            valid_till=None, tax_template_id=None,
        ))
        result = call_action(mod.get_quotation, conn, ns(
            quotation_id=create["quotation_id"],
        ))
        assert is_ok(result)
        assert "items" in result
        assert len(result["items"]) == 1

    def test_get_nonexistent_fails(self, conn):
        result = call_action(mod.get_quotation, conn, ns(
            quotation_id="fake-id",
        ))
        assert is_error(result)


class TestListQuotations:
    def test_list_by_company(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            valid_till=None, tax_template_id=None,
        ))
        result = call_action(mod.list_quotations, conn, ns(
            company_id=env["company_id"], customer_id=None,
            doc_status=None, from_date=None, to_date=None,
            search=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSubmitQuotation:
    def test_submit_draft(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            valid_till=None, tax_template_id=None,
        ))
        result = call_action(mod.submit_quotation, conn, ns(
            quotation_id=create["quotation_id"],
        ))
        assert is_ok(result)

        row = conn.execute("SELECT status FROM quotation WHERE id=?",
                           (create["quotation_id"],)).fetchone()
        assert row["status"] == "open"

    def test_submit_already_submitted_fails(self, conn, env):
        items = _items(env, ("item1", "1", "10.00"))
        create = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            valid_till=None, tax_template_id=None,
        ))
        call_action(mod.submit_quotation, conn, ns(
            quotation_id=create["quotation_id"],
        ))
        result = call_action(mod.submit_quotation, conn, ns(
            quotation_id=create["quotation_id"],
        ))
        assert is_error(result)


class TestConvertQuotationToSO:
    def test_convert_submitted(self, conn, env):
        items = _items(env, ("item1", "5", "100.00"))
        create = call_action(mod.add_quotation, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            valid_till=None, tax_template_id=None,
        ))
        call_action(mod.submit_quotation, conn, ns(
            quotation_id=create["quotation_id"],
        ))
        result = call_action(mod.convert_quotation_to_so, conn, ns(
            quotation_id=create["quotation_id"],
            delivery_date="2026-07-15",
        ))
        assert is_ok(result)
        assert "sales_order_id" in result

        # Verify SO created in DB
        so = conn.execute("SELECT * FROM sales_order WHERE id=?",
                          (result["sales_order_id"],)).fetchone()
        assert so is not None
        assert so["customer_id"] == env["customer"]
        assert so["status"] == "draft"

    def test_convert_nonexistent_fails(self, conn, env):
        result = call_action(mod.convert_quotation_to_so, conn, ns(
            quotation_id="fake-id",
            delivery_date=None,
        ))
        assert is_error(result)
