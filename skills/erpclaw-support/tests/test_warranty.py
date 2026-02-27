"""Tests for warranty claim actions (add, update, list)."""
import pytest
from helpers import (
    _call_action,
    create_test_company,
    create_test_customer,
    create_test_item,
    create_test_warranty_claim,
)
from db_query import ACTIONS


# ── 1. Add warranty claim — all fields ──────────────────────────────────────

def test_add_warranty_claim_all_fields(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    item_id = create_test_item(fresh_db)

    result = _call_action(
        ACTIONS["add-warranty-claim"],
        fresh_db,
        customer_id=customer_id,
        complaint_description="Screen cracked",
        item_id=item_id,
        warranty_expiry_date="2027-12-31",
    )

    assert result["status"] == "ok"
    claim = result["warranty_claim"]
    assert claim["naming_series"].startswith("WC-")
    assert claim["status"] == "open"
    assert claim["cost"] == "0"
    assert claim["complaint_description"] == "Screen cracked"
    assert claim["item_id"] == item_id
    assert claim["warranty_expiry_date"] == "2027-12-31"
    assert claim["customer_id"] == customer_id


# ── 2. Add warranty claim — minimal (customer + description only) ───────────

def test_add_warranty_claim_minimal(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)

    result = _call_action(
        ACTIONS["add-warranty-claim"],
        fresh_db,
        customer_id=customer_id,
        complaint_description="Device overheating",
    )

    assert result["status"] == "ok"
    claim = result["warranty_claim"]
    assert claim["customer_id"] == customer_id
    assert claim["complaint_description"] == "Device overheating"
    assert claim["item_id"] is None
    assert claim["warranty_expiry_date"] is None


# ── 3. Update warranty claim — resolution and cost ──────────────────────────

def test_update_warranty_with_resolution_and_cost(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)
    claim_id = create_test_warranty_claim(fresh_db, customer_id=customer_id)

    result = _call_action(
        ACTIONS["update-warranty-claim"],
        fresh_db,
        warranty_claim_id=claim_id,
        resolution="repair",
        cost="250.00",
        status="resolved",
        resolution_date="2026-02-16",
    )

    assert result["status"] == "ok"
    claim = result["warranty_claim"]
    assert claim["resolution"] == "repair"
    assert claim["cost"] == "250.00"
    assert claim["status"] == "resolved"
    assert claim["resolution_date"] == "2026-02-16"


# ── 4. List warranty claims — filter by customer ────────────────────────────

def test_list_warranty_claims_by_customer(fresh_db):
    company_id = create_test_company(fresh_db)
    customer1_id = create_test_customer(fresh_db, company_id=company_id,
                                        customer_name="Customer One")
    customer2_id = create_test_customer(fresh_db, company_id=company_id,
                                        customer_name="Customer Two")

    # 2 claims for customer 1
    create_test_warranty_claim(fresh_db, customer_id=customer1_id,
                               complaint_description="Issue A")
    create_test_warranty_claim(fresh_db, customer_id=customer1_id,
                               complaint_description="Issue B")
    # 1 claim for customer 2
    create_test_warranty_claim(fresh_db, customer_id=customer2_id,
                               complaint_description="Issue C")

    result = _call_action(
        ACTIONS["list-warranty-claims"],
        fresh_db,
        customer_id=customer1_id,
    )

    assert result["status"] == "ok"
    assert result["total"] == 2


# ── 5. List warranty claims — filter by status ──────────────────────────────

def test_list_warranty_claims_by_status(fresh_db):
    company_id = create_test_company(fresh_db)
    customer_id = create_test_customer(fresh_db, company_id=company_id)

    claim1_id = create_test_warranty_claim(fresh_db, customer_id=customer_id,
                                            complaint_description="Problem 1")
    claim2_id = create_test_warranty_claim(fresh_db, customer_id=customer_id,
                                            complaint_description="Problem 2")

    # Resolve claim 1
    _call_action(
        ACTIONS["update-warranty-claim"],
        fresh_db,
        warranty_claim_id=claim1_id,
        status="resolved",
        resolution="repair",
    )

    # List open — should be 1
    open_result = _call_action(
        ACTIONS["list-warranty-claims"],
        fresh_db,
        status="open",
    )
    assert open_result["status"] == "ok"
    assert open_result["total"] == 1

    # List resolved — should be 1
    resolved_result = _call_action(
        ACTIONS["list-warranty-claims"],
        fresh_db,
        status="resolved",
    )
    assert resolved_result["status"] == "ok"
    assert resolved_result["total"] == 1
