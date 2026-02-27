"""Tests for naming series actions.

Test IDs: GL-NS-01 through GL-NS-04
"""
import db_query
from helpers import _call_action, create_test_company


# ---------------------------------------------------------------------------
# GL-NS-01: seed-naming-series creates entries for all entity types
# ---------------------------------------------------------------------------
def test_seed_naming_series_creates_entries(fresh_db):
    company_id = create_test_company(fresh_db)
    result = _call_action(
        db_query.seed_naming_series, fresh_db,
        company_id=company_id,
    )
    assert result["status"] == "ok"
    assert result["series_created"] > 0
    assert result["series_created"] == result["total_types"]

    # Verify entries in DB
    count = fresh_db.execute(
        "SELECT COUNT(*) as cnt FROM naming_series WHERE company_id = ?",
        (company_id,),
    ).fetchone()["cnt"]
    assert count == result["total_types"]


# ---------------------------------------------------------------------------
# GL-NS-02: seed-naming-series is idempotent
# ---------------------------------------------------------------------------
def test_seed_naming_series_idempotent(fresh_db):
    company_id = create_test_company(fresh_db)

    first = _call_action(
        db_query.seed_naming_series, fresh_db,
        company_id=company_id,
    )
    first_created = first["series_created"]
    assert first_created > 0

    second = _call_action(
        db_query.seed_naming_series, fresh_db,
        company_id=company_id,
    )
    assert second["status"] == "ok"
    assert second["series_created"] == 0


# ---------------------------------------------------------------------------
# GL-NS-03: next-series returns formatted name
# ---------------------------------------------------------------------------
def test_next_series_returns_formatted_name(fresh_db):
    company_id = create_test_company(fresh_db)
    # Seed first
    _call_action(
        db_query.seed_naming_series, fresh_db,
        company_id=company_id,
    )

    result = _call_action(
        db_query.next_series, fresh_db,
        entity_type="sales_invoice",
        company_id=company_id,
    )
    assert result["status"] == "ok"
    assert result["entity_type"] == "sales_invoice"
    # Format: INV-YEAR-00001
    name = result["series"]
    assert name.startswith("INV-")
    assert name.endswith("-00001")


# ---------------------------------------------------------------------------
# GL-NS-04: next-series increments
# ---------------------------------------------------------------------------
def test_next_series_increments(fresh_db):
    company_id = create_test_company(fresh_db)
    _call_action(
        db_query.seed_naming_series, fresh_db,
        company_id=company_id,
    )

    first = _call_action(
        db_query.next_series, fresh_db,
        entity_type="journal_entry",
        company_id=company_id,
    )
    second = _call_action(
        db_query.next_series, fresh_db,
        entity_type="journal_entry",
        company_id=company_id,
    )

    assert first["series"].endswith("-00001")
    assert second["series"].endswith("-00002")
