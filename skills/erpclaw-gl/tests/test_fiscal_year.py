"""Tests for fiscal year actions.

Test IDs: GL-FY-01 through GL-FY-04
"""
import db_query
from helpers import _call_action, create_test_company


# ---------------------------------------------------------------------------
# GL-FY-01: add-fiscal-year creates FY
# ---------------------------------------------------------------------------
def test_add_fiscal_year(fresh_db):
    company_id = create_test_company(fresh_db)
    result = _call_action(
        db_query.add_fiscal_year, fresh_db,
        company_id=company_id, name="FY 2026",
        start_date="2026-01-01", end_date="2026-12-31",
    )
    assert result["status"] == "ok"
    assert "fiscal_year_id" in result
    assert result["name"] == "FY 2026"

    # Verify in DB
    row = fresh_db.execute(
        "SELECT * FROM fiscal_year WHERE id = ?", (result["fiscal_year_id"],)
    ).fetchone()
    assert row["name"] == "FY 2026"
    assert row["start_date"] == "2026-01-01"
    assert row["end_date"] == "2026-12-31"
    assert row["is_closed"] == 0
    assert row["company_id"] == company_id


# ---------------------------------------------------------------------------
# GL-FY-02: Overlapping fiscal years rejected
# ---------------------------------------------------------------------------
def test_overlapping_fiscal_years_rejected(fresh_db):
    company_id = create_test_company(fresh_db)
    first = _call_action(
        db_query.add_fiscal_year, fresh_db,
        company_id=company_id, name="FY 2026",
        start_date="2026-01-01", end_date="2026-12-31",
    )
    assert first["status"] == "ok"

    # Try to create overlapping FY
    second = _call_action(
        db_query.add_fiscal_year, fresh_db,
        company_id=company_id, name="FY 2026 Overlap",
        start_date="2026-06-01", end_date="2027-05-31",
    )
    assert second["status"] == "error"
    assert "overlap" in second["message"].lower()


# ---------------------------------------------------------------------------
# GL-FY-03: list-fiscal-years returns all
# ---------------------------------------------------------------------------
def test_list_fiscal_years(fresh_db):
    company_id = create_test_company(fresh_db)
    _call_action(
        db_query.add_fiscal_year, fresh_db,
        company_id=company_id, name="FY 2025",
        start_date="2025-01-01", end_date="2025-12-31",
    )
    _call_action(
        db_query.add_fiscal_year, fresh_db,
        company_id=company_id, name="FY 2026",
        start_date="2026-01-01", end_date="2026-12-31",
    )

    result = _call_action(
        db_query.list_fiscal_years, fresh_db, company_id=company_id,
    )
    assert result["status"] == "ok"
    assert len(result["fiscal_years"]) == 2
    names = [fy["name"] for fy in result["fiscal_years"]]
    assert "FY 2025" in names
    assert "FY 2026" in names


# ---------------------------------------------------------------------------
# GL-FY-04: validate-period-close on empty FY
# ---------------------------------------------------------------------------
def test_validate_period_close_empty_fy(fresh_db):
    company_id = create_test_company(fresh_db)
    fy = _call_action(
        db_query.add_fiscal_year, fresh_db,
        company_id=company_id, name="FY 2026",
        start_date="2026-01-01", end_date="2026-12-31",
    )
    fy_id = fy["fiscal_year_id"]

    result = _call_action(
        db_query.validate_period_close, fresh_db,
        fiscal_year_id=fy_id,
    )
    assert result["status"] == "ok"
    assert result["fiscal_year"] == "FY 2026"
    # No entries, so P&L should be zero
    assert result["net_income"] == "0"
    assert result["trial_balance_balanced"] is True
