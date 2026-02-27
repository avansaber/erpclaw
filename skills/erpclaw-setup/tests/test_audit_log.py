"""Tests for audit log actions.

Test IDs: S-AL-01 through S-AL-02
"""
import db_query
from helpers import _call_action


# ---------------------------------------------------------------------------
# S-AL-01: Create a company, then verify audit log has an entry
# ---------------------------------------------------------------------------
def test_audit_log_created(fresh_db):
    create = _call_action(db_query.setup_company, fresh_db, name="Audit Co")
    company_id = create["company_id"]

    result = _call_action(
        db_query.get_audit_log, fresh_db,
        entity_type="company", entity_id=company_id
    )

    assert result["status"] == "ok"
    assert len(result["entries"]) >= 1

    entry = result["entries"][0]
    assert entry["entity_type"] == "company"
    assert entry["entity_id"] == company_id
    assert entry["action"] == "create"
    assert entry["skill"] == "erpclaw-setup"
    assert entry["new_values"]["name"] == "Audit Co"


# ---------------------------------------------------------------------------
# S-AL-02: Multiple actions, filter by entity_type
# ---------------------------------------------------------------------------
def test_audit_log_filters(fresh_db):
    # Create a company (produces entity_type="company" audit entry)
    _call_action(db_query.setup_company, fresh_db, name="Filter Co")

    # Add a currency (produces entity_type="currency" audit entry)
    _call_action(
        db_query.add_currency, fresh_db,
        code="JPY", name="Japanese Yen"
    )

    # Add a UoM (produces entity_type="uom" audit entry)
    _call_action(db_query.add_uom, fresh_db, name="Kilogram")

    # Filter for company entries only
    company_log = _call_action(
        db_query.get_audit_log, fresh_db,
        entity_type="company"
    )
    assert company_log["status"] == "ok"
    assert len(company_log["entries"]) == 1
    assert all(e["entity_type"] == "company" for e in company_log["entries"])

    # Filter for currency entries only
    currency_log = _call_action(
        db_query.get_audit_log, fresh_db,
        entity_type="currency"
    )
    assert currency_log["status"] == "ok"
    assert len(currency_log["entries"]) == 1
    assert all(e["entity_type"] == "currency" for e in currency_log["entries"])

    # Unfiltered: should have at least 3 entries
    all_log = _call_action(db_query.get_audit_log, fresh_db)
    assert all_log["status"] == "ok"
    assert len(all_log["entries"]) >= 3
