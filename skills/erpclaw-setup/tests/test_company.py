"""Tests for company management actions.

Test IDs: S-C-01 through S-C-06
"""
import db_query
from helpers import _call_action


# ---------------------------------------------------------------------------
# S-C-01: Create a company and verify the response
# ---------------------------------------------------------------------------
def test_setup_company(fresh_db):
    result = _call_action(db_query.setup_company, fresh_db, name="Acme Corp")

    assert result["status"] == "ok"
    assert result["name"] == "Acme Corp"
    assert result["abbr"] == "AC"
    assert "company_id" in result
    assert len(result["company_id"]) == 36  # UUID format


# ---------------------------------------------------------------------------
# S-C-02: Company is created with default currency=USD and country=US
# ---------------------------------------------------------------------------
def test_setup_company_defaults(fresh_db):
    result = _call_action(db_query.setup_company, fresh_db, name="Default Corp")

    # Verify defaults by reading the row directly
    row = fresh_db.execute(
        "SELECT * FROM company WHERE id = ?", (result["company_id"],)
    ).fetchone()

    assert row["default_currency"] == "USD"
    assert row["country"] == "United States"
    assert row["fiscal_year_start_month"] == 1
    assert row["perpetual_inventory"] == 1
    assert row["enable_negative_stock"] == 0


# ---------------------------------------------------------------------------
# S-C-03: Create a company, then get it back and verify fields
# ---------------------------------------------------------------------------
def test_get_company(fresh_db):
    create = _call_action(db_query.setup_company, fresh_db, name="GetMe Inc")
    company_id = create["company_id"]

    result = _call_action(db_query.get_company, fresh_db, company_id=company_id)

    assert result["status"] == "ok"
    company = result["company"]
    assert company["id"] == company_id
    assert company["name"] == "GetMe Inc"
    assert company["abbr"] == "GI"
    assert company["default_currency"] == "USD"
    assert company["country"] == "United States"


# ---------------------------------------------------------------------------
# S-C-04: Create 2 companies, list returns both
# ---------------------------------------------------------------------------
def test_list_companies(fresh_db):
    _call_action(db_query.setup_company, fresh_db, name="Alpha Corp")
    _call_action(db_query.setup_company, fresh_db, name="Beta Corp")

    result = _call_action(db_query.list_companies, fresh_db)

    assert result["status"] == "ok"
    names = [c["name"] for c in result["companies"]]
    assert "Alpha Corp" in names
    assert "Beta Corp" in names
    assert len(result["companies"]) == 2


# ---------------------------------------------------------------------------
# S-C-05: Create company, update its name, verify updated_fields
# ---------------------------------------------------------------------------
def test_update_company(fresh_db):
    create = _call_action(db_query.setup_company, fresh_db, name="Old Name LLC")
    company_id = create["company_id"]

    result = _call_action(
        db_query.update_company, fresh_db,
        company_id=company_id, name="New Name LLC"
    )

    assert result["status"] == "ok"
    assert result["company_id"] == company_id
    assert "name" in result["updated_fields"]

    # Verify the actual row was updated
    row = fresh_db.execute(
        "SELECT name FROM company WHERE id = ?", (company_id,)
    ).fetchone()
    assert row["name"] == "New Name LLC"


# ---------------------------------------------------------------------------
# S-C-06: Creating a company with a duplicate name fails
# ---------------------------------------------------------------------------
def test_duplicate_company_name(fresh_db):
    _call_action(db_query.setup_company, fresh_db, name="Unique Corp")
    result = _call_action(db_query.setup_company, fresh_db, name="Unique Corp")

    assert result["status"] == "error"
    assert "failed" in result["message"].lower() or "already" in result["message"].lower()
