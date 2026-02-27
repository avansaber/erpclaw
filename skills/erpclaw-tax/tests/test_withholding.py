"""Tests for withholding actions (add-tax-withholding-category,
get-withholding-details, record-withholding-entry, record-1099-payment)."""
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
    """Create company + liability account + withholding category + supplier."""
    conn = fresh_db
    company_id = create_test_company(conn)
    # Create a liability account that the withholding category needs
    liability_acct = create_test_account(
        conn, company_id, "Tax Payable", "liability",
        account_type="tax",
    )

    # Create withholding category
    cat_result = _call_action(
        ACTIONS["add-tax-withholding-category"], conn,
        name="NEC Withholding",
        wh_rate="30.0",
        threshold_amount="600.00",
        form_type="1099-NEC",
        company_id=company_id,
    )
    assert cat_result["status"] == "ok"
    cat_id = cat_result["category_id"]

    # Create 1099 supplier WITH the category assigned
    sup_id = create_test_supplier(
        conn, company_id, name="Contractor LLC",
        tax_id="12-3456789", is_1099=True, w9_on_file=True,
        wh_category_id=cat_id,
    )

    return {
        "conn": conn,
        "company_id": company_id,
        "liability_acct": liability_acct,
        "wh_cat_id": cat_id,
        "supplier_id": sup_id,
    }


def test_add_tax_withholding_category(fresh_db):
    """Create a withholding category with group."""
    conn = fresh_db
    company_id = create_test_company(conn)
    create_test_account(
        conn, company_id, "Tax Payable", "liability",
        account_type="tax",
    )
    result = _call_action(
        ACTIONS["add-tax-withholding-category"], conn,
        name="Backup Withholding",
        wh_rate="24.0",
        threshold_amount="0",
        form_type="1099-NEC",
        company_id=company_id,
    )
    assert result["status"] == "ok"
    assert result["category_id"]
    assert result["name"] == "Backup Withholding"

    # Verify category in DB
    row = conn.execute(
        "SELECT * FROM tax_withholding_category WHERE id = ?",
        (result["category_id"],),
    ).fetchone()
    assert row is not None
    assert row["category_code"] == "1099-NEC"

    # Verify group was created with rate
    grp = conn.execute(
        "SELECT * FROM tax_withholding_group WHERE category_id = ?",
        (result["category_id"],),
    ).fetchone()
    assert grp is not None
    assert grp["rate"] == "24.0"


def test_get_withholding_details(setup):
    """Get withholding details for a supplier with no entries yet."""
    s = setup
    result = _call_action(
        ACTIONS["get-withholding-details"], s["conn"],
        supplier_id=s["supplier_id"],
        tax_year="2026",
        company_id=s["company_id"],
    )
    assert result["status"] == "ok"
    assert result["is_1099_vendor"] is True
    assert result["withholding_category"] == "NEC Withholding"
    assert Decimal(result["ytd_payments"]) == Decimal("0.00")
    assert result["w9_on_file"] is True
    # With W9 on file, no backup withholding
    assert Decimal(result["backup_withholding_rate"]) == Decimal("0")


def test_record_withholding_entry(setup):
    """Record a withholding entry and verify it's stored."""
    s = setup
    voucher_id = str(uuid.uuid4())
    result = _call_action(
        ACTIONS["record-withholding-entry"], s["conn"],
        supplier_id=s["supplier_id"],
        voucher_type="purchase_invoice",
        voucher_id=voucher_id,
        withholding_amount="150.00",
        tax_year="2026",
    )
    assert result["status"] == "ok"
    assert result["withholding_entry_id"]

    # Verify in DB
    row = s["conn"].execute(
        "SELECT * FROM tax_withholding_entry WHERE id = ?",
        (result["withholding_entry_id"],),
    ).fetchone()
    assert row is not None
    assert row["party_type"] == "supplier"
    assert row["withheld_amount"] == "150.00"


def test_record_1099_payment(setup):
    """Record a 1099-reportable payment and check YTD total."""
    s = setup
    voucher_id = str(uuid.uuid4())
    result = _call_action(
        ACTIONS["record-1099-payment"], s["conn"],
        supplier_id=s["supplier_id"],
        ple_amount="2500.00",
        tax_year="2026",
        voucher_type="payment_entry",
        voucher_id=voucher_id,
    )
    assert result["status"] == "ok"
    assert Decimal(result["ytd_1099_total"]) == Decimal("2500.00")

    # Record another payment and verify accumulation
    voucher_id_2 = str(uuid.uuid4())
    result2 = _call_action(
        ACTIONS["record-1099-payment"], s["conn"],
        supplier_id=s["supplier_id"],
        ple_amount="1500.00",
        tax_year="2026",
        voucher_type="payment_entry",
        voucher_id=voucher_id_2,
    )
    assert result2["status"] == "ok"
    assert Decimal(result2["ytd_1099_total"]) == Decimal("4000.00")
