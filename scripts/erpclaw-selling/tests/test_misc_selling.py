"""Tests for erpclaw-selling miscellaneous actions.

Actions tested:
  - add-sales-partner, list-sales-partners
  - add-recurring-template, update-recurring-template
  - list-recurring-templates, generate-recurring-invoices
  - status
"""
import json
import pytest
from decimal import Decimal
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company,
)

mod = load_db_query()


def _items(env, *specs):
    """Build items JSON. Each spec = (item_key, qty, rate)."""
    return json.dumps([
        {"item_id": env[k], "qty": q, "rate": r}
        for k, q, r in specs
    ])


# ──────────────────────────────────────────────────────────────────────────────
# Sales Partners
# ──────────────────────────────────────────────────────────────────────────────

class TestAddSalesPartner:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_sales_partner, conn, ns(
            name="Channel Partner A", company_id=env["company_id"],
            commission_rate="10.00",
        ))
        assert is_ok(result)
        assert "sales_partner_id" in result

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_sales_partner, conn, ns(
            name=None, company_id=env["company_id"],
            commission_rate="5.00",
        ))
        assert is_error(result)


class TestListSalesPartners:
    def test_list(self, conn, env):
        call_action(mod.add_sales_partner, conn, ns(
            name="Partner X", company_id=env["company_id"],
            commission_rate="5.00",
        ))
        result = call_action(mod.list_sales_partners, conn, ns(
            company_id=env["company_id"],
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Recurring Templates
# ──────────────────────────────────────────────────────────────────────────────

class TestAddRecurringTemplate:
    def test_basic_create(self, conn, env):
        items = _items(env, ("item1", "1", "500.00"))
        result = call_action(mod.add_recurring_template, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            items=items, frequency="monthly",
            start_date="2026-01-01", end_date="2026-12-31",
            tax_template_id=None, payment_terms_id=None,
        ))
        assert is_ok(result)
        assert "template_id" in result

    def test_missing_customer_fails(self, conn, env):
        items = _items(env, ("item1", "1", "100.00"))
        result = call_action(mod.add_recurring_template, conn, ns(
            customer_id=None, company_id=env["company_id"],
            items=items, frequency="monthly",
            start_date="2026-01-01", end_date="2026-12-31",
            tax_template_id=None, payment_terms_id=None,
        ))
        assert is_error(result)

    def test_invalid_frequency_fails(self, conn, env):
        items = _items(env, ("item1", "1", "100.00"))
        result = call_action(mod.add_recurring_template, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            items=items, frequency="daily",
            start_date="2026-01-01", end_date="2026-12-31",
            tax_template_id=None, payment_terms_id=None,
        ))
        assert is_error(result)


class TestListRecurringTemplates:
    def test_list(self, conn, env):
        items = _items(env, ("item1", "1", "100.00"))
        call_action(mod.add_recurring_template, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            items=items, frequency="quarterly",
            start_date="2026-01-01", end_date="2026-12-31",
            tax_template_id=None, payment_terms_id=None,
        ))
        result = call_action(mod.list_recurring_templates, conn, ns(
            company_id=env["company_id"], customer_id=None,
            template_status=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestGenerateRecurringInvoices:
    def test_generate(self, conn, env):
        items = _items(env, ("item1", "1", "500.00"))
        create = call_action(mod.add_recurring_template, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            items=items, frequency="monthly",
            start_date="2026-01-01", end_date="2026-12-31",
            tax_template_id=None, payment_terms_id=None,
        ))
        result = call_action(mod.generate_recurring_invoices, conn, ns(
            as_of_date="2026-03-31", company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert "invoices_generated" in result


# ──────────────────────────────────────────────────────────────────────────────
# Status
# ──────────────────────────────────────────────────────────────────────────────

class TestStatus:
    def test_status_empty(self, conn, env):
        result = call_action(mod.status_action, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)

    def test_status_with_data(self, conn, env):
        # Create some data first
        items = _items(env, ("item1", "5", "100.00"))
        call_action(mod.add_sales_order, conn, ns(
            customer_id=env["customer"], company_id=env["company_id"],
            posting_date="2026-06-15", items=items,
            delivery_date="2026-07-01", tax_template_id=None,
        ))
        result = call_action(mod.status_action, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
