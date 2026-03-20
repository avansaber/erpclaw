"""Tests for erpclaw-accounting-adv Lease Accounting (ASC 842) actions.

Actions tested: add-lease, update-lease, get-lease, list-leases, classify-lease,
                calculate-rou-asset, calculate-lease-liability,
                generate-amortization-schedule, record-lease-payment,
                lease-maturity-report, lease-disclosure-report, lease-summary
"""
import json
import pytest
from decimal import Decimal
from advacct_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _add_lease(conn, env, term_months=24, monthly_payment="1000.00",
               discount_rate="0.05", start_date="2026-01-01", end_date="2027-12-31"):
    return call_action(mod.add_lease, conn, ns(
        company_id=env["company_id"], lessee_name="Acme Corp",
        lessor_name="Property Holdings LLC",
        asset_description="Office space 5th floor",
        lease_type="operating", start_date=start_date, end_date=end_date,
        term_months=term_months, monthly_payment=monthly_payment,
        annual_escalation="0.03", discount_rate=discount_rate,
        purchase_option_price=None,
    ))


# ──────────────────────────────────────────────────────────────────────────────
# Leases
# ──────────────────────────────────────────────────────────────────────────────

class TestAddLease:
    def test_basic_create(self, conn, env):
        result = _add_lease(conn, env)
        assert is_ok(result)
        assert result["lessee_name"] == "Acme Corp"
        assert result["lessor_name"] == "Property Holdings LLC"
        assert result["lease_status"] == "draft"
        assert result["term_months"] == 24

    def test_missing_lessee_fails(self, conn, env):
        result = call_action(mod.add_lease, conn, ns(
            company_id=env["company_id"], lessee_name=None,
            lessor_name="Test", asset_description=None,
            lease_type=None, start_date=None, end_date=None,
            term_months=12, monthly_payment="500",
            annual_escalation=None, discount_rate=None,
            purchase_option_price=None,
        ))
        assert is_error(result)

    def test_missing_lessor_fails(self, conn, env):
        result = call_action(mod.add_lease, conn, ns(
            company_id=env["company_id"], lessee_name="Test",
            lessor_name=None, asset_description=None,
            lease_type=None, start_date=None, end_date=None,
            term_months=12, monthly_payment="500",
            annual_escalation=None, discount_rate=None,
            purchase_option_price=None,
        ))
        assert is_error(result)


class TestUpdateLease:
    def test_update_name(self, conn, env):
        lease = _add_lease(conn, env)
        result = call_action(mod.update_lease, conn, ns(
            id=lease["id"], lessee_name="Updated Corp",
            lessor_name=None, asset_description=None,
            start_date=None, end_date=None,
            monthly_payment=None, discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type=None, term_months=None,
        ))
        assert is_ok(result)
        assert "lessee_name" in result["updated_fields"]

    def test_no_fields_fails(self, conn, env):
        lease = _add_lease(conn, env)
        result = call_action(mod.update_lease, conn, ns(
            id=lease["id"], lessee_name=None,
            lessor_name=None, asset_description=None,
            start_date=None, end_date=None,
            monthly_payment=None, discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type=None, term_months=None,
        ))
        assert is_error(result)

    def test_invalid_lease_type_fails(self, conn, env):
        lease = _add_lease(conn, env)
        result = call_action(mod.update_lease, conn, ns(
            id=lease["id"], lessee_name=None,
            lessor_name=None, asset_description=None,
            start_date=None, end_date=None,
            monthly_payment=None, discount_rate=None,
            annual_escalation=None, purchase_option_price=None,
            lease_type="invalid", term_months=None,
        ))
        assert is_error(result)


class TestGetLease:
    def test_get(self, conn, env):
        lease = _add_lease(conn, env)
        result = call_action(mod.get_lease, conn, ns(id=lease["id"]))
        assert is_ok(result)
        assert result["lessee_name"] == "Acme Corp"
        assert "payments" in result
        assert "amortization_entries" in result

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_lease, conn, ns(id="fake-id"))
        assert is_error(result)


class TestListLeases:
    def test_list(self, conn, env):
        _add_lease(conn, env)
        result = call_action(mod.list_leases, conn, ns(
            company_id=env["company_id"], lease_type=None,
            lease_status=None, search=None, limit=50, offset=0,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1

    def test_list_by_type(self, conn, env):
        _add_lease(conn, env)
        result = call_action(mod.list_leases, conn, ns(
            company_id=env["company_id"], lease_type="operating",
            lease_status=None, search=None, limit=50, offset=0,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Classification & Calculations
# ──────────────────────────────────────────────────────────────────────────────

class TestClassifyLease:
    def test_auto_classify_short_term(self, conn, env):
        lease = _add_lease(conn, env, term_months=24)
        result = call_action(mod.classify_lease, conn, ns(
            id=lease["id"], lease_type=None,
        ))
        assert is_ok(result)
        assert result["lease_type"] == "operating"

    def test_auto_classify_long_term(self, conn, env):
        lease = _add_lease(conn, env, term_months=48)
        result = call_action(mod.classify_lease, conn, ns(
            id=lease["id"], lease_type=None,
        ))
        assert is_ok(result)
        assert result["lease_type"] == "finance"

    def test_manual_override(self, conn, env):
        lease = _add_lease(conn, env, term_months=24)
        result = call_action(mod.classify_lease, conn, ns(
            id=lease["id"], lease_type="finance",
        ))
        assert is_ok(result)
        assert result["lease_type"] == "finance"


class TestCalculateRouAsset:
    def test_calculate(self, conn, env):
        lease = _add_lease(conn, env, term_months=24, monthly_payment="1000.00",
                           discount_rate="0.06")
        result = call_action(mod.calculate_rou_asset, conn, ns(id=lease["id"]))
        assert is_ok(result)
        rou = Decimal(result["rou_asset_value"])
        assert rou > Decimal("0")
        # PV of $1000/mo for 24 months at 6%/yr ≈ $22,562
        assert rou > Decimal("22000") and rou < Decimal("24000")


class TestCalculateLeaseLiability:
    def test_calculate(self, conn, env):
        lease = _add_lease(conn, env, term_months=24, monthly_payment="1000.00",
                           discount_rate="0.06")
        result = call_action(mod.calculate_lease_liability, conn, ns(id=lease["id"]))
        assert is_ok(result)
        liability = Decimal(result["lease_liability"])
        assert liability > Decimal("0")

    def test_rou_equals_liability_at_inception(self, conn, env):
        lease = _add_lease(conn, env, term_months=12, monthly_payment="2000.00",
                           discount_rate="0.05")
        rou_result = call_action(mod.calculate_rou_asset, conn, ns(id=lease["id"]))
        liab_result = call_action(mod.calculate_lease_liability, conn, ns(id=lease["id"]))
        assert Decimal(rou_result["rou_asset_value"]) == Decimal(liab_result["lease_liability"])


# ──────────────────────────────────────────────────────────────────────────────
# Amortization & Payments
# ──────────────────────────────────────────────────────────────────────────────

class TestGenerateAmortizationSchedule:
    def test_generate(self, conn, env):
        lease = _add_lease(conn, env, term_months=12, monthly_payment="1000.00",
                           discount_rate="0.06", start_date="2026-01-01")
        result = call_action(mod.generate_amortization_schedule, conn, ns(
            lease_id=lease["id"],
        ))
        assert is_ok(result)
        assert result["entries_created"] == 12
        assert Decimal(result["initial_balance"]) > Decimal("0")

    def test_missing_start_date_fails(self, conn, env):
        lease = _add_lease(conn, env, start_date=None)
        result = call_action(mod.generate_amortization_schedule, conn, ns(
            lease_id=lease["id"],
        ))
        assert is_error(result)


class TestRecordLeasePayment:
    def test_record_payment(self, conn, env):
        lease = _add_lease(conn, env, term_months=12, monthly_payment="1000.00",
                           discount_rate="0.06")
        # Calculate liability first so balance is available
        call_action(mod.calculate_lease_liability, conn, ns(id=lease["id"]))
        result = call_action(mod.record_lease_payment, conn, ns(
            lease_id=lease["id"], payment_date="2026-02-01",
            payment_amount="1000.00",
        ))
        assert is_ok(result)
        assert result["payment_status"] == "paid"
        assert Decimal(result["payment_amount"]) == Decimal("1000.00")
        assert Decimal(result["principal"]) > Decimal("0")
        assert Decimal(result["interest"]) >= Decimal("0")

    def test_missing_payment_date_fails(self, conn, env):
        lease = _add_lease(conn, env)
        result = call_action(mod.record_lease_payment, conn, ns(
            lease_id=lease["id"], payment_date=None,
            payment_amount="1000.00",
        ))
        assert is_error(result)

    def test_missing_amount_fails(self, conn, env):
        lease = _add_lease(conn, env)
        result = call_action(mod.record_lease_payment, conn, ns(
            lease_id=lease["id"], payment_date="2026-02-01",
            payment_amount=None,
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Reports
# ──────────────────────────────────────────────────────────────────────────────

class TestLeaseMaturityReport:
    def test_report(self, conn, env):
        _add_lease(conn, env)
        result = call_action(mod.lease_maturity_report, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["total_leases"] >= 1


class TestLeaseDisclosureReport:
    def test_report(self, conn, env):
        _add_lease(conn, env)
        result = call_action(mod.lease_disclosure_report, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert len(result["rows"]) >= 1


class TestLeaseSummary:
    def test_summary(self, conn, env):
        _add_lease(conn, env)
        result = call_action(mod.lease_summary, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["total_leases"] >= 1
        assert "by_status" in result
        assert "by_type" in result
