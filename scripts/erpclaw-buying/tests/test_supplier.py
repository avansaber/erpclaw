"""Tests for erpclaw-buying supplier management.

Actions tested: add-supplier, update-supplier, get-supplier, list-suppliers
"""
import pytest
from buying_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_supplier,
)

mod = load_db_query()


class TestAddSupplier:
    def test_basic_create(self, conn, env):
        result = call_action(mod.add_supplier, conn, ns(
            name="New Supplier", company_id=env["company_id"],
            supplier_type=None, supplier_group=None,
            payment_terms_id=None, tax_id=None,
            is_1099_vendor=None, primary_address=None,
        ))
        assert is_ok(result)
        assert "supplier_id" in result
        assert result["name"] == "New Supplier"

    def test_individual_type(self, conn, env):
        result = call_action(mod.add_supplier, conn, ns(
            name="John Vendor", company_id=env["company_id"],
            supplier_type="individual", supplier_group=None,
            payment_terms_id=None, tax_id=None,
            is_1099_vendor=None, primary_address=None,
        ))
        assert is_ok(result)

    def test_missing_name_fails(self, conn, env):
        result = call_action(mod.add_supplier, conn, ns(
            name=None, company_id=env["company_id"],
            supplier_type=None, supplier_group=None,
            payment_terms_id=None, tax_id=None,
            is_1099_vendor=None, primary_address=None,
        ))
        assert is_error(result)

    def test_missing_company_fails(self, conn):
        result = call_action(mod.add_supplier, conn, ns(
            name="No Company Supplier", company_id=None,
            supplier_type=None, supplier_group=None,
            payment_terms_id=None, tax_id=None,
            is_1099_vendor=None, primary_address=None,
        ))
        assert is_error(result)


class TestUpdateSupplier:
    def test_update_name(self, conn, env):
        result = call_action(mod.update_supplier, conn, ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            name="Updated Supplier", supplier_group=None,
            supplier_type=None, payment_terms_id=None,
            tax_id=None, is_1099_vendor=None, primary_address=None,
        ))
        assert is_ok(result)
        assert "name" in result.get("updated_fields", [])

    def test_update_no_fields_fails(self, conn, env):
        result = call_action(mod.update_supplier, conn, ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
            name=None, supplier_group=None,
            supplier_type=None, payment_terms_id=None,
            tax_id=None, is_1099_vendor=None, primary_address=None,
        ))
        assert is_error(result)


class TestGetSupplier:
    def test_get_by_id(self, conn, env):
        result = call_action(mod.get_supplier, conn, ns(
            supplier_id=env["supplier"], company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["id"] == env["supplier"]

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_supplier, conn, ns(
            supplier_id="fake-id", company_id=env["company_id"],
        ))
        assert is_error(result)


class TestListSuppliers:
    def test_list(self, conn, env):
        result = call_action(mod.list_suppliers, conn, ns(
            company_id=env["company_id"], search=None,
            supplier_group=None, limit=None, offset=None,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1

    def test_list_search(self, conn, env):
        result = call_action(mod.list_suppliers, conn, ns(
            company_id=env["company_id"], search="Acme",
            supplier_group=None, limit=None, offset=None,
        ))
        assert result["total_count"] >= 1
