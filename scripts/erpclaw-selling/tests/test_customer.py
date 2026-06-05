"""Tests for erpclaw-selling customer management actions.

Actions tested:
  - add-customer
  - update-customer
  - get-customer
  - list-customers
"""
import json
import pytest
from decimal import Decimal
from selling_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_customer,
)

mod = load_db_query()


class TestAddCustomer:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_customer, conn, ns(
            name="New Customer", company_id=env["company_id"],
            customer_type=None, customer_group=None,
            payment_terms_id=None, credit_limit=None,
            tax_id=None, exempt_from_sales_tax=None,
            primary_address=None, primary_contact=None,
        ))
        assert is_ok(result)
        assert result["name"] == "New Customer"
        assert result["customer_type"] == "company"
        assert "customer_id" in result

    def test_individual_type(self, conn, env):
        result = call_action(mod.add_customer, conn, ns(
            name="Jane Doe", company_id=env["company_id"],
            customer_type="individual", customer_group=None,
            payment_terms_id=None, credit_limit="5000.00",
            tax_id=None, exempt_from_sales_tax=None,
            primary_address=None, primary_contact=None,
        ))
        assert is_ok(result)
        assert result["customer_type"] == "individual"

        row = conn.execute(
            "SELECT credit_limit FROM customer WHERE id=?",
            (result["customer_id"],)
        ).fetchone()
        assert Decimal(row["credit_limit"]) == Decimal("5000.00")

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_customer, conn, ns(
            name=None, company_id=env["company_id"],
            customer_type=None, customer_group=None,
            payment_terms_id=None, credit_limit=None,
            tax_id=None, exempt_from_sales_tax=None,
            primary_address=None, primary_contact=None,
        ))
        assert is_error(result)

    def test_missing_company_fails(self, conn):
        result = call_action(mod.add_customer, conn, ns(
            name="No Company", company_id=None,
            customer_type=None, customer_group=None,
            payment_terms_id=None, credit_limit=None,
            tax_id=None, exempt_from_sales_tax=None,
            primary_address=None, primary_contact=None,
        ))
        assert is_error(result)

    def test_invalid_type_fails(self, conn, env):
        result = call_action(mod.add_customer, conn, ns(
            name="Bad Type", company_id=env["company_id"],
            customer_type="invalid", customer_group=None,
            payment_terms_id=None, credit_limit=None,
            tax_id=None, exempt_from_sales_tax=None,
            primary_address=None, primary_contact=None,
        ))
        assert is_error(result)


class TestUpdateCustomer:
    def test_update_name(self, conn, env):
        result = call_action(mod.update_customer, conn, ns(
            customer_id=env["customer"],
            name="Updated Name", credit_limit=None,
            payment_terms_id=None, customer_group=None,
            customer_type=None,
        ))
        assert is_ok(result)
        assert "name" in result["updated_fields"]

        row = conn.execute("SELECT name FROM customer WHERE id=?",
                           (env["customer"],)).fetchone()
        assert row["name"] == "Updated Name"

    def test_update_credit_limit(self, conn, env):
        result = call_action(mod.update_customer, conn, ns(
            customer_id=env["customer"],
            name=None, credit_limit="50000.00",
            payment_terms_id=None, customer_group=None,
            customer_type=None,
        ))
        assert is_ok(result)
        assert "credit_limit" in result["updated_fields"]

    def test_update_no_fields_fails(self, conn, env):
        result = call_action(mod.update_customer, conn, ns(
            customer_id=env["customer"],
            name=None, credit_limit=None,
            payment_terms_id=None, customer_group=None,
            customer_type=None,
        ))
        assert is_error(result)

    def test_update_nonexistent_fails(self, conn):
        result = call_action(mod.update_customer, conn, ns(
            customer_id="fake-id",
            name="New", credit_limit=None,
            payment_terms_id=None, customer_group=None,
            customer_type=None,
        ))
        assert is_error(result)


class TestGetCustomer:
    def test_get_by_id(self, conn, env):
        result = call_action(mod.get_customer, conn, ns(
            customer_id=env["customer"],
        ))
        assert is_ok(result)
        assert result["id"] == env["customer"]
        assert "total_outstanding" in result
        assert "outstanding_invoice_count" in result

    def test_get_nonexistent_fails(self, conn):
        result = call_action(mod.get_customer, conn, ns(
            customer_id="fake-id",
        ))
        assert is_error(result)

    def test_get_missing_id_fails(self, conn):
        result = call_action(mod.get_customer, conn, ns(
            customer_id=None,
        ))
        assert is_error(result)


class TestCustomerEmailPhone:
    """ADR-0012: dedicated email + phone columns (FINDING-003)."""

    def test_add_stores_and_returns_plain_email(self, conn, env):
        result = call_action(mod.add_customer, conn, ns(
            name="Wayne Enterprises", company_id=env["company_id"],
            customer_type=None, customer_group=None,
            payment_terms_id=None, credit_limit=None,
            tax_id=None, exempt_from_sales_tax=None,
            primary_address=None, primary_contact=None,
            email="bruce@we.com", phone="555-0100",
        ))
        assert is_ok(result)
        # stored verbatim (plain string, NOT a JSON blob)
        row = conn.execute(
            "SELECT email, phone FROM customer WHERE id=?",
            (result["customer_id"],)).fetchone()
        assert row["email"] == "bruce@we.com"
        assert row["phone"] == "555-0100"

        got = call_action(mod.get_customer, conn, ns(
            customer_id=result["customer_id"]))
        assert is_ok(got)
        assert got["email"] == "bruce@we.com"
        assert got["phone"] == "555-0100"

    def test_add_without_email_is_null(self, conn, env):
        result = call_action(mod.add_customer, conn, ns(
            name="No Email Co", company_id=env["company_id"],
            customer_type=None, customer_group=None,
            payment_terms_id=None, credit_limit=None,
            tax_id=None, exempt_from_sales_tax=None,
            primary_address=None, primary_contact=None,
        ))
        assert is_ok(result)
        row = conn.execute(
            "SELECT email, phone FROM customer WHERE id=?",
            (result["customer_id"],)).fetchone()
        assert row["email"] is None
        assert row["phone"] is None

    def test_update_email_and_phone(self, conn, env):
        result = call_action(mod.update_customer, conn, ns(
            customer_id=env["customer"],
            name=None, credit_limit=None,
            payment_terms_id=None, customer_group=None,
            customer_type=None, email="new@we.com", phone="555-0200",
        ))
        assert is_ok(result)
        assert "email" in result["updated_fields"]
        assert "phone" in result["updated_fields"]
        row = conn.execute(
            "SELECT email, phone FROM customer WHERE id=?",
            (env["customer"],)).fetchone()
        assert row["email"] == "new@we.com"
        assert row["phone"] == "555-0200"

    def test_dunning_resolves_email_column(self, conn, env):
        result = call_action(mod.add_customer, conn, ns(
            name="Dunning Target", company_id=env["company_id"],
            customer_type=None, customer_group=None,
            payment_terms_id=None, credit_limit=None,
            tax_id=None, exempt_from_sales_tax=None,
            primary_address=None, primary_contact=None,
            email="ar@dunning.com", phone=None,
        ))
        assert is_ok(result)
        resolved = mod._resolve_customer_email(conn, result["customer_id"])
        assert resolved == "ar@dunning.com"


class TestListCustomers:
    def test_list_by_company(self, conn, env):
        result = call_action(mod.list_customers, conn, ns(
            company_id=env["company_id"],
            customer_group=None, search=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1
        assert len(result["customers"]) >= 1

    def test_list_empty(self, conn):
        cid = seed_company(conn)
        result = call_action(mod.list_customers, conn, ns(
            company_id=cid,
            customer_group=None, search=None,
            limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] == 0

    def test_list_search(self, conn, env):
        result = call_action(mod.list_customers, conn, ns(
            company_id=env["company_id"],
            customer_group=None, search="Acme",
            limit=None, offset=None,
        ))
        assert result["total_count"] >= 1
