"""Tests for recurring AP bill template actions.

Actions tested:
  - add-recurring-bill-template
  - list-recurring-bill-templates
  - generate-recurring-bills
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


def _common_ns(**overrides):
    """Build namespace with common defaults for recurring bill actions."""
    defaults = dict(
        supplier_id=None, company_id=None,
        items=None, frequency=None,
        start_date=None, end_date=None,
        tax_template_id=None, auto_submit=False,
        posting_date=None, name=None,
        blanket_order_id=None, blanket_status=None,
        sales_order_id=None,
        template_id=None, as_of_date=None,
        template_status=None,
        limit="20", offset="0",
    )
    defaults.update(overrides)
    return ns(**defaults)


# --------------------------------------------------------------------------
# add-recurring-bill-template
# --------------------------------------------------------------------------

class TestAddRecurringBillTemplate:
    def test_basic_create(self, conn, env):
        items = _items(env, ("item1", "1", "500.00"))
        result = call_action(mod.add_recurring_bill_template, conn, _common_ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            items=items, frequency="monthly",
            start_date="2026-01-01", end_date="2026-12-31",
        ))
        assert is_ok(result)
        assert "template_id" in result
        assert result["frequency"] == "monthly"
        assert result["next_bill_date"] == "2026-01-01"

    def test_missing_supplier_fails(self, conn, env):
        items = _items(env, ("item1", "1", "100.00"))
        result = call_action(mod.add_recurring_bill_template, conn, _common_ns(
            supplier_id=None, company_id=env["company_id"],
            items=items, frequency="monthly",
            start_date="2026-01-01",
        ))
        assert is_error(result)

    def test_invalid_frequency_fails(self, conn, env):
        items = _items(env, ("item1", "1", "100.00"))
        result = call_action(mod.add_recurring_bill_template, conn, _common_ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            items=items, frequency="daily",
            start_date="2026-01-01",
        ))
        assert is_error(result)

    def test_missing_items_fails(self, conn, env):
        result = call_action(mod.add_recurring_bill_template, conn, _common_ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            items=None, frequency="monthly",
            start_date="2026-01-01",
        ))
        assert is_error(result)

    def test_quarterly_frequency(self, conn, env):
        items = _items(env, ("item1", "1", "3000.00"))
        result = call_action(mod.add_recurring_bill_template, conn, _common_ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            items=items, frequency="quarterly",
            start_date="2026-04-01",
        ))
        assert is_ok(result)
        assert result["frequency"] == "quarterly"


# --------------------------------------------------------------------------
# list-recurring-bill-templates
# --------------------------------------------------------------------------

class TestListRecurringBillTemplates:
    def test_list(self, conn, env):
        items = _items(env, ("item1", "1", "500.00"))
        call_action(mod.add_recurring_bill_template, conn, _common_ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            items=items, frequency="monthly",
            start_date="2026-01-01",
        ))
        result = call_action(mod.list_recurring_bill_templates, conn, _common_ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1

    def test_filter_by_supplier(self, conn, env):
        items = _items(env, ("item1", "1", "500.00"))
        call_action(mod.add_recurring_bill_template, conn, _common_ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            items=items, frequency="monthly",
            start_date="2026-01-01",
        ))
        result = call_action(mod.list_recurring_bill_templates, conn, _common_ns(
            company_id=env["company_id"],
            supplier_id=env["supplier"],
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# --------------------------------------------------------------------------
# generate-recurring-bills
# --------------------------------------------------------------------------

class TestGenerateRecurringBills:
    def _create_active_template(self, conn, env, start="2026-01-01",
                                 end="2026-12-31", frequency="monthly"):
        items = _items(env, ("item1", "1", "500.00"))
        tmpl = call_action(mod.add_recurring_bill_template, conn, _common_ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            items=items, frequency=frequency,
            start_date=start, end_date=end,
        ))
        assert is_ok(tmpl)
        # Activate the template
        conn.execute(
            "UPDATE recurring_bill_template SET status = 'active' WHERE id = ?",
            (tmpl["template_id"],)
        )
        conn.commit()
        return tmpl

    def test_generate_bill(self, conn, env):
        tmpl = self._create_active_template(conn, env)
        result = call_action(mod.generate_recurring_bills, conn, _common_ns(
            company_id=env["company_id"],
            as_of_date="2026-01-15",
        ))
        assert is_ok(result)
        assert result["bills_generated"] >= 1
        bill = result["bills"][0]
        assert bill["supplier_id"] == env["supplier"]
        assert Decimal(bill["amount"]) == Decimal("500.00")

    def test_next_date_advances(self, conn, env):
        tmpl = self._create_active_template(conn, env)
        call_action(mod.generate_recurring_bills, conn, _common_ns(
            company_id=env["company_id"],
            as_of_date="2026-01-15",
        ))
        # Verify next_bill_date advanced to 2026-02-01
        row = conn.execute(
            "SELECT next_bill_date FROM recurring_bill_template WHERE id = ?",
            (tmpl["template_id"],)).fetchone()
        assert row["next_bill_date"] == "2026-02-01"

    def test_not_due_yet_skipped(self, conn, env):
        self._create_active_template(conn, env, start="2026-06-01")
        result = call_action(mod.generate_recurring_bills, conn, _common_ns(
            company_id=env["company_id"],
            as_of_date="2026-01-15",
        ))
        assert is_ok(result)
        assert result["bills_generated"] == 0

    def test_template_completed(self, conn, env):
        """Template whose end_date is before next bill date should be completed."""
        self._create_active_template(conn, env,
            start="2026-12-01", end="2026-12-31", frequency="monthly")
        result = call_action(mod.generate_recurring_bills, conn, _common_ns(
            company_id=env["company_id"],
            as_of_date="2026-12-15",
        ))
        assert is_ok(result)
        assert result["bills_generated"] == 1
        assert result["templates_completed"] == 1

    def test_generate_with_auto_submit(self, conn, env):
        items = _items(env, ("item1", "1", "500.00"))
        tmpl = call_action(mod.add_recurring_bill_template, conn, _common_ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            items=items, frequency="monthly",
            start_date="2026-01-01", end_date="2026-12-31",
            auto_submit=True,
        ))
        assert is_ok(tmpl)
        # Activate
        conn.execute(
            "UPDATE recurring_bill_template SET status = 'active' WHERE id = ?",
            (tmpl["template_id"],)
        )
        conn.commit()

        result = call_action(mod.generate_recurring_bills, conn, _common_ns(
            company_id=env["company_id"],
            as_of_date="2026-01-15",
        ))
        assert is_ok(result)
        assert result["bills_generated"] >= 1
        bill = result["bills"][0]
        assert bill["status"] == "submitted"
        assert bill["naming_series"] is not None
