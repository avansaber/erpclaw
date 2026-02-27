"""Tests for customer actions: add-customer, update-customer, get-customer, list-customers."""
import json
import pytest
from helpers import (
    _call_action,
    setup_selling_environment,
    create_test_company,
    create_test_customer,
)
from db_query import ACTIONS


class TestAddCustomer:
    """Tests for the add-customer action."""

    def test_add_customer(self, fresh_db):
        """Create a company-type customer with minimal fields and verify success."""
        company_id = create_test_company(fresh_db)

        result = _call_action(
            ACTIONS["add-customer"], fresh_db,
            name="Acme Corp",
            company_id=company_id,
        )
        assert result["status"] == "ok"
        assert "customer_id" in result
        assert result["name"] == "Acme Corp"
        assert result["customer_type"] == "company"

        # Verify persistence in the database
        row = fresh_db.execute(
            "SELECT * FROM customer WHERE id = ?", (result["customer_id"],)
        ).fetchone()
        assert row is not None
        assert row["name"] == "Acme Corp"
        assert row["customer_type"] == "company"
        assert row["status"] == "active"
        assert row["company_id"] == company_id

    def test_add_customer_individual(self, fresh_db):
        """Create an individual-type customer and verify the customer_type is stored."""
        company_id = create_test_company(fresh_db)

        result = _call_action(
            ACTIONS["add-customer"], fresh_db,
            name="Jane Doe",
            company_id=company_id,
            customer_type="individual",
        )
        assert result["status"] == "ok"
        assert result["customer_type"] == "individual"
        assert result["name"] == "Jane Doe"

        row = fresh_db.execute(
            "SELECT * FROM customer WHERE id = ?", (result["customer_id"],)
        ).fetchone()
        assert row["customer_type"] == "individual"

    def test_add_customer_missing_name(self, fresh_db):
        """Creating a customer without --name should return an error."""
        company_id = create_test_company(fresh_db)

        result = _call_action(
            ACTIONS["add-customer"], fresh_db,
            company_id=company_id,
        )
        assert result["status"] == "error"
        assert "name" in result["message"].lower()


class TestGetCustomer:
    """Tests for the get-customer action."""

    def test_get_customer(self, fresh_db):
        """Create a customer, then retrieve it and verify all returned fields."""
        company_id = create_test_company(fresh_db)

        # Create via the action so we exercise the full code path
        add_result = _call_action(
            ACTIONS["add-customer"], fresh_db,
            name="Beta Industries",
            company_id=company_id,
            customer_type="company",
            credit_limit="50000.00",
        )
        assert add_result["status"] == "ok"
        customer_id = add_result["customer_id"]

        result = _call_action(
            ACTIONS["get-customer"], fresh_db,
            customer_id=customer_id,
        )
        assert result["status"] == "ok"
        assert result["id"] == customer_id
        assert result["name"] == "Beta Industries"
        assert result["customer_type"] == "company"
        assert result["credit_limit"] == "50000.00"
        assert result["company_id"] == company_id
        # No invoices yet, so outstanding should be zero
        assert result["total_outstanding"] == "0.00"
        assert result["outstanding_invoice_count"] == 0


class TestListCustomers:
    """Tests for the list-customers action."""

    def test_list_customers(self, fresh_db):
        """Create two customers and verify both appear in the list."""
        company_id = create_test_company(fresh_db)

        cust1_id = create_test_customer(fresh_db, company_id, name="Alpha Corp")
        cust2_id = create_test_customer(fresh_db, company_id, name="Zeta LLC")

        result = _call_action(
            ACTIONS["list-customers"], fresh_db,
            company_id=company_id,
        )
        assert result["status"] == "ok"
        assert result["total_count"] == 2
        assert len(result["customers"]) == 2

        returned_ids = {c["id"] for c in result["customers"]}
        assert cust1_id in returned_ids
        assert cust2_id in returned_ids


class TestUpdateCustomer:
    """Tests for the update-customer action."""

    def test_update_customer(self, fresh_db):
        """Update a customer's payment_terms_id and verify the updated_fields."""
        company_id = create_test_company(fresh_db)
        customer_id = create_test_customer(fresh_db, company_id, name="Update Target")

        # Create a payment terms row so FK is satisfied
        import uuid
        pt_id = str(uuid.uuid4())
        fresh_db.execute(
            "INSERT INTO payment_terms (id, name) VALUES (?, ?)",
            (pt_id, "Net 30"),
        )
        fresh_db.commit()

        result = _call_action(
            ACTIONS["update-customer"], fresh_db,
            customer_id=customer_id,
            payment_terms_id=pt_id,
        )
        assert result["status"] == "ok"
        assert result["customer_id"] == customer_id
        assert "payment_terms_id" in result["updated_fields"]

        # Verify in database
        row = fresh_db.execute(
            "SELECT payment_terms_id FROM customer WHERE id = ?", (customer_id,)
        ).fetchone()
        assert row["payment_terms_id"] == pt_id


class TestCustomerCreditLimit:
    """Tests for customer credit limit handling."""

    def test_customer_credit_limit(self, fresh_db):
        """Create a customer with a credit_limit and verify it is stored correctly."""
        company_id = create_test_company(fresh_db)

        result = _call_action(
            ACTIONS["add-customer"], fresh_db,
            name="Credit Customer",
            company_id=company_id,
            credit_limit="100000.00",
        )
        assert result["status"] == "ok"
        customer_id = result["customer_id"]

        row = fresh_db.execute(
            "SELECT credit_limit FROM customer WHERE id = ?", (customer_id,)
        ).fetchone()
        assert row["credit_limit"] == "100000.00"

        # Also verify via get-customer
        get_result = _call_action(
            ACTIONS["get-customer"], fresh_db,
            customer_id=customer_id,
        )
        assert get_result["status"] == "ok"
        assert get_result["credit_limit"] == "100000.00"


class TestCustomerTaxExemption:
    """Tests for customer tax exemption flag."""

    def test_customer_tax_exemption(self, fresh_db):
        """Create a customer with exempt_from_sales_tax and verify the flag is set."""
        company_id = create_test_company(fresh_db)

        result = _call_action(
            ACTIONS["add-customer"], fresh_db,
            name="Tax Exempt Org",
            company_id=company_id,
            exempt_from_sales_tax="1",
        )
        assert result["status"] == "ok"
        customer_id = result["customer_id"]

        row = fresh_db.execute(
            "SELECT exempt_from_sales_tax FROM customer WHERE id = ?",
            (customer_id,),
        ).fetchone()
        assert row["exempt_from_sales_tax"] == 1

        # Verify via get-customer
        get_result = _call_action(
            ACTIONS["get-customer"], fresh_db,
            customer_id=customer_id,
        )
        assert get_result["status"] == "ok"
        assert get_result["exempt_from_sales_tax"] == 1
