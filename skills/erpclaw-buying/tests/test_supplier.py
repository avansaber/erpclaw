"""Tests for supplier actions: add-supplier, update-supplier, get-supplier, list-suppliers."""
import json
import uuid

import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_supplier,
    setup_buying_environment,
)
from db_query import ACTIONS


class TestAddSupplier:
    """Tests for the add-supplier action."""

    def test_add_supplier(self, fresh_db):
        """Create a supplier with required fields and verify success."""
        company_id = create_test_company(fresh_db)

        result = _call_action(
            ACTIONS["add-supplier"], fresh_db,
            name="Acme Supply Co",
            company_id=company_id,
        )
        assert result["status"] == "ok"
        assert "supplier_id" in result
        assert result["name"] == "Acme Supply Co"

        # Verify persisted in database
        row = fresh_db.execute(
            "SELECT * FROM supplier WHERE id = ?", (result["supplier_id"],)
        ).fetchone()
        assert row is not None
        assert row["name"] == "Acme Supply Co"
        assert row["supplier_type"] == "company"  # default
        assert row["status"] == "active"
        assert row["company_id"] == company_id

    def test_add_supplier_individual(self, fresh_db):
        """Create a supplier with supplier_type=individual."""
        company_id = create_test_company(fresh_db)

        result = _call_action(
            ACTIONS["add-supplier"], fresh_db,
            name="John Doe",
            company_id=company_id,
            supplier_type="individual",
        )
        assert result["status"] == "ok"
        assert "supplier_id" in result

        row = fresh_db.execute(
            "SELECT * FROM supplier WHERE id = ?", (result["supplier_id"],)
        ).fetchone()
        assert row["supplier_type"] == "individual"
        assert row["name"] == "John Doe"

    def test_add_supplier_missing_name(self, fresh_db):
        """Creating a supplier without --name should return an error."""
        company_id = create_test_company(fresh_db)

        result = _call_action(
            ACTIONS["add-supplier"], fresh_db,
            company_id=company_id,
        )
        assert result["status"] == "error"
        assert "name" in result["message"].lower()


class TestGetSupplier:
    """Tests for the get-supplier action."""

    def test_get_supplier(self, fresh_db):
        """Create a supplier, then retrieve it and verify returned fields."""
        company_id = create_test_company(fresh_db)
        supplier_id = create_test_supplier(fresh_db, company_id, name="Retrieval Corp")

        result = _call_action(
            ACTIONS["get-supplier"], fresh_db,
            supplier_id=supplier_id,
        )
        assert result["status"] == "ok"
        assert result["id"] == supplier_id
        assert result["name"] == "Retrieval Corp"
        assert result["supplier_type"] == "company"
        assert result["company_id"] == company_id
        # Outstanding should default to zero with no invoices
        assert result["total_outstanding"] == "0.00"
        assert result["outstanding_invoice_count"] == 0


class TestListSuppliers:
    """Tests for the list-suppliers action."""

    def test_list_suppliers(self, fresh_db):
        """Create 2 suppliers and verify list returns both."""
        company_id = create_test_company(fresh_db)
        create_test_supplier(fresh_db, company_id, name="Alpha Supplies")
        create_test_supplier(fresh_db, company_id, name="Beta Materials")

        result = _call_action(
            ACTIONS["list-suppliers"], fresh_db,
            company_id=company_id,
        )
        assert result["status"] == "ok"
        assert result["total_count"] == 2
        assert len(result["suppliers"]) == 2
        names = {s["name"] for s in result["suppliers"]}
        assert "Alpha Supplies" in names
        assert "Beta Materials" in names


class TestUpdateSupplier:
    """Tests for the update-supplier action."""

    def test_update_supplier(self, fresh_db):
        """Update a supplier name and verify updated_fields."""
        company_id = create_test_company(fresh_db)
        supplier_id = create_test_supplier(fresh_db, company_id, name="Old Name Inc")

        result = _call_action(
            ACTIONS["update-supplier"], fresh_db,
            supplier_id=supplier_id,
            name="New Name Inc",
        )
        assert result["status"] == "ok"
        assert result["supplier_id"] == supplier_id
        assert "name" in result["updated_fields"]

        # Verify in database
        row = fresh_db.execute(
            "SELECT * FROM supplier WHERE id = ?", (supplier_id,)
        ).fetchone()
        assert row["name"] == "New Name Inc"


class TestSupplier1099Flag:
    """Tests for the is_1099_vendor flag on suppliers."""

    def test_supplier_1099_flag(self, fresh_db):
        """Create a supplier with is_1099_vendor=1 and verify it is stored."""
        company_id = create_test_company(fresh_db)

        result = _call_action(
            ACTIONS["add-supplier"], fresh_db,
            name="Contractor LLC",
            company_id=company_id,
            is_1099_vendor="1",
        )
        assert result["status"] == "ok"
        assert "supplier_id" in result

        row = fresh_db.execute(
            "SELECT * FROM supplier WHERE id = ?", (result["supplier_id"],)
        ).fetchone()
        assert row["is_1099_vendor"] == 1


class TestSupplierWithPaymentTerms:
    """Tests for creating a supplier linked to payment terms."""

    def test_supplier_with_payment_terms(self, fresh_db):
        """Create payment terms, then a supplier referencing those terms."""
        company_id = create_test_company(fresh_db)

        # Create payment terms directly via SQL
        pt_id = str(uuid.uuid4())
        fresh_db.execute(
            """INSERT INTO payment_terms (id, name, due_days)
               VALUES (?, ?, ?)""",
            (pt_id, "Net 30", 30),
        )
        fresh_db.commit()

        result = _call_action(
            ACTIONS["add-supplier"], fresh_db,
            name="Terms Supplier Corp",
            company_id=company_id,
            payment_terms_id=pt_id,
        )
        assert result["status"] == "ok"
        assert "supplier_id" in result

        row = fresh_db.execute(
            "SELECT * FROM supplier WHERE id = ?", (result["supplier_id"],)
        ).fetchone()
        assert row["payment_terms_id"] == pt_id
