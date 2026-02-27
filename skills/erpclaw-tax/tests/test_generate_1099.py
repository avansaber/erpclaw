"""Tests for generate-1099-data action."""
import json
import uuid
from decimal import Decimal
import pytest

from db_query import ACTIONS
from helpers import (
    _call_action,
    create_test_company,
    create_test_account,
    create_test_supplier,
)


@pytest.fixture
def setup(fresh_db):
    """Create company + category + suppliers + payments for 1099 tests."""
    conn = fresh_db
    company_id = create_test_company(conn)
    liability_acct = create_test_account(
        conn, company_id, "Tax Payable", "liability",
        account_type="tax",
    )

    # Create withholding category with $600 threshold
    cat_result = _call_action(
        ACTIONS["add-tax-withholding-category"], conn,
        name="1099-NEC Reporting",
        wh_rate="0",
        threshold_amount="600.00",
        form_type="1099-NEC",
        company_id=company_id,
    )
    cat_id = cat_result["category_id"]

    # Supplier A: paid $1000 (above threshold)
    sup_a = create_test_supplier(
        conn, company_id, name="Big Contractor",
        tax_id="11-1111111", is_1099=True, w9_on_file=True,
        wh_category_id=cat_id,
    )
    # Record 1099 payment for sup_a
    _call_action(
        ACTIONS["record-1099-payment"], conn,
        supplier_id=sup_a,
        ple_amount="1000.00",
        tax_year="2026",
        voucher_type="payment_entry",
        voucher_id=str(uuid.uuid4()),
    )

    # Supplier B: paid $400 (below threshold)
    sup_b = create_test_supplier(
        conn, company_id, name="Small Contractor",
        tax_id="22-2222222", is_1099=True, w9_on_file=True,
        wh_category_id=cat_id,
    )
    _call_action(
        ACTIONS["record-1099-payment"], conn,
        supplier_id=sup_b,
        ple_amount="400.00",
        tax_year="2026",
        voucher_type="payment_entry",
        voucher_id=str(uuid.uuid4()),
    )

    return {
        "conn": conn,
        "company_id": company_id,
        "cat_id": cat_id,
        "sup_a": sup_a,
        "sup_b": sup_b,
    }


def test_generate_1099_data(setup):
    """Generate 1099 data, should include supplier above threshold."""
    s = setup
    result = _call_action(
        ACTIONS["generate-1099-data"], s["conn"],
        tax_year="2026",
        company_id=s["company_id"],
    )
    assert result["status"] == "ok"
    # Only supplier A should be included (above $600 threshold)
    assert len(result["vendors"]) == 1
    vendor = result["vendors"][0]
    assert vendor["supplier_id"] == s["sup_a"]
    assert vendor["name"] == "Big Contractor"
    assert vendor["tin"] == "11-1111111"
    assert Decimal(vendor["total_paid"]) == Decimal("1000.00")
    assert vendor["form_type"] == "1099-NEC"
    assert Decimal(vendor["box_1"]) == Decimal("1000.00")


def test_generate_1099_threshold_filter(setup):
    """Suppliers below threshold should NOT appear in 1099 data."""
    s = setup
    result = _call_action(
        ACTIONS["generate-1099-data"], s["conn"],
        tax_year="2026",
        company_id=s["company_id"],
    )
    assert result["status"] == "ok"
    vendor_ids = [v["supplier_id"] for v in result["vendors"]]
    assert s["sup_b"] not in vendor_ids
