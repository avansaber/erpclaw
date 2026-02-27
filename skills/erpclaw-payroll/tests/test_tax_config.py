"""Tests for tax configuration actions."""
import json
from decimal import Decimal
from helpers import _call_action, setup_payroll_environment
from db_query import ACTIONS


def test_add_federal_tax_brackets(fresh_db):
    """Create federal income tax brackets."""
    rates = json.dumps([
        {"from_amount": "0", "to_amount": "11600", "rate": "10"},
        {"from_amount": "11600", "to_amount": "47150", "rate": "12"},
        {"from_amount": "47150", "to_amount": "100525", "rate": "22"},
    ])
    result = _call_action(ACTIONS["add-income-tax-slab"], fresh_db,
                          name="2026 Federal Single",
                          tax_jurisdiction="federal",
                          filing_status="single",
                          effective_from="2026-01-01",
                          standard_deduction="14600",
                          rates=rates)
    assert result["status"] == "ok"
    assert "income_tax_slab_id" in result


def test_update_fica_config(fresh_db):
    """Set FICA rates for a tax year."""
    result = _call_action(ACTIONS["update-fica-config"], fresh_db,
                          tax_year="2026",
                          ss_wage_base="168600",
                          ss_employee_rate="6.2",
                          ss_employer_rate="6.2",
                          medicare_employee_rate="1.45",
                          medicare_employer_rate="1.45",
                          additional_medicare_threshold="200000",
                          additional_medicare_rate="0.9")
    assert result["status"] == "ok"
    assert result["tax_year"] == 2026


def test_fica_config_upsert(fresh_db):
    """FICA config upsert updates existing record."""
    _call_action(ACTIONS["update-fica-config"], fresh_db,
                 tax_year="2026", ss_wage_base="168600",
                 ss_employee_rate="6.2", ss_employer_rate="6.2",
                 medicare_employee_rate="1.45", medicare_employer_rate="1.45",
                 additional_medicare_threshold="200000", additional_medicare_rate="0.9")

    # Update with new wage base
    result = _call_action(ACTIONS["update-fica-config"], fresh_db,
                          tax_year="2026", ss_wage_base="176100",
                          ss_employee_rate="6.2", ss_employer_rate="6.2",
                          medicare_employee_rate="1.45", medicare_employer_rate="1.45",
                          additional_medicare_threshold="200000", additional_medicare_rate="0.9")
    assert result["status"] == "ok"

    # Should still be one record
    count = fresh_db.execute("SELECT COUNT(*) as cnt FROM fica_config WHERE tax_year=2026").fetchone()
    assert count["cnt"] == 1
    row = fresh_db.execute("SELECT ss_wage_base FROM fica_config WHERE tax_year=2026").fetchone()
    assert Decimal(row["ss_wage_base"]) == Decimal("176100")


def test_update_futa_config(fresh_db):
    """Set FUTA config (federal unemployment)."""
    result = _call_action(ACTIONS["update-futa-suta-config"], fresh_db,
                          tax_year="2026",
                          wage_base="7000",
                          rate="6.0")
    assert result["status"] == "ok"


def test_update_suta_config_with_state(fresh_db):
    """Set SUTA config for a specific state."""
    result = _call_action(ACTIONS["update-futa-suta-config"], fresh_db,
                          tax_year="2026",
                          state_code="CA",
                          wage_base="7000",
                          rate="3.4",
                          employer_rate_override="2.1")
    assert result["status"] == "ok"
