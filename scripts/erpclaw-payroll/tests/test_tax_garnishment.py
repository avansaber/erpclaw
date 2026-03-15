"""Tests for erpclaw-payroll tax configuration and wage garnishment actions.

Actions tested:
  - add-income-tax-slab
  - update-fica-config
  - update-futa-suta-config
  - add-garnishment
  - update-garnishment
  - list-garnishments
  - get-garnishment
"""
import json
import pytest
from payroll_helpers import (
    call_action, ns, is_error, is_ok,
    seed_company, seed_employee, load_db_query,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# add-income-tax-slab
# ──────────────────────────────────────────────────────────────────────────────

class TestAddIncomeTaxSlab:
    def test_basic_create(self, conn):
        """Create a federal income tax slab with rate brackets."""
        rates_json = json.dumps([
            {"from_amount": "0", "to_amount": "11600", "rate": "10"},
            {"from_amount": "11600", "to_amount": "47150", "rate": "12"},
            {"from_amount": "47150", "to_amount": None, "rate": "22"},
        ])

        result = call_action(mod.add_income_tax_slab, conn, ns(
            name="2026 Federal Single",
            tax_jurisdiction="federal",
            effective_from="2026-01-01",
            filing_status="single",
            state_code=None,
            standard_deduction="14600",
            rates=rates_json,
        ))
        assert is_ok(result)
        assert result["name"] == "2026 Federal Single"
        assert result["tax_jurisdiction"] == "federal"
        assert result["filing_status"] == "single"
        assert result["standard_deduction"] == "14600.00"
        assert result["rate_count"] == 3
        assert "income_tax_slab_id" in result

        # Verify slab in DB
        slab_id = result["income_tax_slab_id"]
        row = conn.execute(
            "SELECT * FROM income_tax_slab WHERE id = ?", (slab_id,),
        ).fetchone()
        assert row is not None
        assert row["is_active"] == 1

        # Verify rate brackets in DB
        rate_count = conn.execute(
            "SELECT COUNT(*) FROM income_tax_slab_rate WHERE slab_id = ?",
            (slab_id,),
        ).fetchone()[0]
        assert rate_count == 3


# ──────────────────────────────────────────────────────────────────────────────
# update-fica-config
# ──────────────────────────────────────────────────────────────────────────────

class TestUpdateFicaConfig:
    def test_update(self, conn):
        """Create/update FICA config for a tax year."""
        result = call_action(mod.update_fica_config, conn, ns(
            tax_year="2026",
            ss_wage_base="168600",
            ss_employee_rate="6.2",
            ss_employer_rate="6.2",
            medicare_employee_rate="1.45",
            medicare_employer_rate="1.45",
            additional_medicare_threshold="200000",
            additional_medicare_rate="0.9",
        ))
        assert is_ok(result)
        assert result["tax_year"] == 2026
        assert result["ss_wage_base"] == "168600.00"
        assert result["ss_employee_rate"] == "6.20"
        assert result["action"] == "created"

        # Verify in DB
        row = conn.execute(
            "SELECT * FROM fica_config WHERE tax_year = 2026",
        ).fetchone()
        assert row is not None
        assert row["ss_wage_base"] == "168600.00"

        # Update the same year (upsert)
        result2 = call_action(mod.update_fica_config, conn, ns(
            tax_year="2026",
            ss_wage_base="170000",
            ss_employee_rate="6.2",
            ss_employer_rate="6.2",
            medicare_employee_rate="1.45",
            medicare_employer_rate="1.45",
            additional_medicare_threshold="200000",
            additional_medicare_rate="0.9",
        ))
        assert is_ok(result2)
        assert result2["action"] == "updated"
        assert result2["ss_wage_base"] == "170000.00"


# ──────────────────────────────────────────────────────────────────────────────
# update-futa-suta-config
# ──────────────────────────────────────────────────────────────────────────────

class TestUpdateFutaSutaConfig:
    def test_update(self, conn):
        """Create/update FUTA (federal unemployment) config."""
        result = call_action(mod.update_futa_suta_config, conn, ns(
            tax_year="2026",
            wage_base="7000",
            rate="6.0",
            state_code=None,
            employer_rate_override=None,
        ))
        assert is_ok(result)
        assert result["tax_year"] == 2026
        assert result["config_type"] == "FUTA (federal)"
        assert result["wage_base"] == "7000.00"
        assert result["rate"] == "6.00"
        assert result["action"] == "created"

        # Verify in DB
        row = conn.execute(
            "SELECT * FROM futa_suta_config WHERE tax_year = 2026 AND state_code IS NULL",
        ).fetchone()
        assert row is not None


# ──────────────────────────────────────────────────────────────────────────────
# add-garnishment
# ──────────────────────────────────────────────────────────────────────────────

class TestAddGarnishment:
    def test_basic_create(self, conn, env):
        """Add a child support garnishment for an employee."""
        result = call_action(mod.add_garnishment, conn, ns(
            employee_id=env["employee_id"],
            order_number="CS-2026-001",
            creditor_name="State Child Support Agency",
            garnishment_type="child_support",
            amount_or_percentage="500",
            is_percentage=False,
            total_owed="12000",
            start_date="2026-01-01",
            end_date="2027-12-31",
        ))
        assert is_ok(result)
        assert "garnishment_id" in result
        assert result["priority"] == 1  # child_support has highest priority

        # Verify in DB
        row = conn.execute(
            "SELECT * FROM wage_garnishment WHERE id = ?",
            (result["garnishment_id"],),
        ).fetchone()
        assert row is not None
        assert row["garnishment_type"] == "child_support"
        assert row["status"] == "active"
        assert row["amount_or_percentage"] == "500"
        assert row["employee_id"] == env["employee_id"]


# ──────────────────────────────────────────────────────────────────────────────
# update-garnishment
# ──────────────────────────────────────────────────────────────────────────────

class TestUpdateGarnishment:
    def test_update(self, conn, env):
        """Update a garnishment status and amount."""
        # First create one
        add_result = call_action(mod.add_garnishment, conn, ns(
            employee_id=env["employee_id"],
            order_number="TL-2026-001",
            creditor_name="IRS",
            garnishment_type="tax_levy",
            amount_or_percentage="300",
            is_percentage=False,
            total_owed="5000",
            start_date="2026-02-01",
            end_date=None,
        ))
        assert is_ok(add_result)
        g_id = add_result["garnishment_id"]

        # Update the garnishment
        result = call_action(mod.update_garnishment, conn, ns(
            garnishment_id=g_id,
            status="paused",
            amount_or_percentage="400",
            total_owed=None,
            end_date=None,
        ))
        assert is_ok(result)
        assert result["updated"] is True

        # Verify in DB
        row = conn.execute(
            "SELECT * FROM wage_garnishment WHERE id = ?", (g_id,),
        ).fetchone()
        assert row["status"] == "paused"
        assert row["amount_or_percentage"] == "400"


# ──────────────────────────────────────────────────────────────────────────────
# list-garnishments
# ──────────────────────────────────────────────────────────────────────────────

class TestListGarnishments:
    def test_list(self, conn, env):
        """List garnishments for an employee."""
        # Create a garnishment
        call_action(mod.add_garnishment, conn, ns(
            employee_id=env["employee_id"],
            order_number="SL-2026-001",
            creditor_name="Dept of Education",
            garnishment_type="student_loan",
            amount_or_percentage="200",
            is_percentage=False,
            total_owed="20000",
            start_date="2026-03-01",
            end_date=None,
        ))

        result = call_action(mod.list_garnishments, conn, ns(
            employee_id=env["employee_id"],
            company_id=None,
            status=None,
        ))
        assert is_ok(result)
        assert result["count"] >= 1
        assert len(result["garnishments"]) >= 1


# ──────────────────────────────────────────────────────────────────────────────
# get-garnishment
# ──────────────────────────────────────────────────────────────────────────────

class TestGetGarnishment:
    def test_get(self, conn, env):
        """Retrieve a single garnishment by ID."""
        add_result = call_action(mod.add_garnishment, conn, ns(
            employee_id=env["employee_id"],
            order_number="CR-2026-001",
            creditor_name="Credit Corp",
            garnishment_type="creditor",
            amount_or_percentage="15",
            is_percentage=True,
            total_owed="8000",
            start_date="2026-04-01",
            end_date=None,
        ))
        assert is_ok(add_result)
        g_id = add_result["garnishment_id"]

        result = call_action(mod.get_garnishment, conn, ns(
            garnishment_id=g_id,
        ))
        assert is_ok(result)
        assert result["id"] == g_id
        assert result["creditor_name"] == "Credit Corp"
        assert result["garnishment_type"] == "creditor"
        assert result["is_percentage"] == 1
