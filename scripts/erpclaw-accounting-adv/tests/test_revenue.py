"""Tests for erpclaw-accounting-adv Revenue Recognition (ASC 606) actions.

Actions tested: add-revenue-contract, update-revenue-contract, get-revenue-contract,
                list-revenue-contracts, add-performance-obligation, list-performance-obligations,
                satisfy-performance-obligation, add-variable-consideration,
                list-variable-considerations, modify-contract, calculate-revenue-schedule,
                generate-revenue-entries, revenue-waterfall-report, revenue-recognition-summary
"""
import json
import pytest
from decimal import Decimal
from advacct_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
)

mod = load_db_query()


def _add_contract(conn, env, customer_name="Acme Corp", total_value="120000.00",
                  start_date="2026-01-01", end_date="2026-12-31"):
    return call_action(mod.add_revenue_contract, conn, ns(
        company_id=env["company_id"], customer_name=customer_name,
        total_value=total_value, contract_number="C-001",
        start_date=start_date, end_date=end_date,
    ))


def _add_obligation(conn, env, contract_id, name="Software License",
                    standalone_price="60000.00"):
    return call_action(mod.add_performance_obligation, conn, ns(
        contract_id=contract_id, company_id=env["company_id"],
        name=name, standalone_price=standalone_price,
        recognition_method="over_time", recognition_basis="time",
    ))


# ──────────────────────────────────────────────────────────────────────────────
# Revenue Contracts
# ──────────────────────────────────────────────────────────────────────────────

class TestAddRevenueContract:
    def test_basic_create(self, conn, env):
        result = _add_contract(conn, env)
        assert is_ok(result)
        assert result["customer_name"] == "Acme Corp"
        assert result["contract_status"] == "draft"
        assert result["total_value"] == "120000.00"

    def test_missing_company_fails(self, conn, env):
        result = call_action(mod.add_revenue_contract, conn, ns(
            company_id=None, customer_name="Test",
            total_value="100", contract_number=None,
            start_date=None, end_date=None,
        ))
        assert is_error(result)

    def test_missing_customer_name_fails(self, conn, env):
        result = call_action(mod.add_revenue_contract, conn, ns(
            company_id=env["company_id"], customer_name=None,
            total_value="100", contract_number=None,
            start_date=None, end_date=None,
        ))
        assert is_error(result)


class TestUpdateRevenueContract:
    def test_update_name(self, conn, env):
        c = _add_contract(conn, env)
        result = call_action(mod.update_revenue_contract, conn, ns(
            id=c["id"], customer_name="Updated Corp",
            contract_number=None, start_date=None,
            end_date=None, total_value=None, contract_status=None,
        ))
        assert is_ok(result)
        assert "customer_name" in result["updated_fields"]

    def test_update_status(self, conn, env):
        c = _add_contract(conn, env)
        result = call_action(mod.update_revenue_contract, conn, ns(
            id=c["id"], customer_name=None,
            contract_number=None, start_date=None,
            end_date=None, total_value=None, contract_status="active",
        ))
        assert is_ok(result)
        assert "contract_status" in result["updated_fields"]

    def test_no_fields_fails(self, conn, env):
        c = _add_contract(conn, env)
        result = call_action(mod.update_revenue_contract, conn, ns(
            id=c["id"], customer_name=None,
            contract_number=None, start_date=None,
            end_date=None, total_value=None, contract_status=None,
        ))
        assert is_error(result)

    def test_invalid_status_fails(self, conn, env):
        c = _add_contract(conn, env)
        result = call_action(mod.update_revenue_contract, conn, ns(
            id=c["id"], customer_name=None,
            contract_number=None, start_date=None,
            end_date=None, total_value=None, contract_status="invalid",
        ))
        assert is_error(result)


class TestGetRevenueContract:
    def test_get(self, conn, env):
        c = _add_contract(conn, env)
        result = call_action(mod.get_revenue_contract, conn, ns(id=c["id"]))
        assert is_ok(result)
        assert result["customer_name"] == "Acme Corp"
        assert "obligations" in result
        assert "variable_considerations" in result

    def test_get_nonexistent_fails(self, conn, env):
        result = call_action(mod.get_revenue_contract, conn, ns(id="fake-id"))
        assert is_error(result)


class TestListRevenueContracts:
    def test_list(self, conn, env):
        _add_contract(conn, env)
        result = call_action(mod.list_revenue_contracts, conn, ns(
            company_id=env["company_id"], contract_status=None,
            search=None, limit=50, offset=0,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1

    def test_list_by_status(self, conn, env):
        _add_contract(conn, env)
        result = call_action(mod.list_revenue_contracts, conn, ns(
            company_id=env["company_id"], contract_status="draft",
            search=None, limit=50, offset=0,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Performance Obligations
# ──────────────────────────────────────────────────────────────────────────────

class TestAddPerformanceObligation:
    def test_basic_create(self, conn, env):
        c = _add_contract(conn, env)
        result = _add_obligation(conn, env, c["id"])
        assert is_ok(result)
        assert result["name"] == "Software License"
        assert result["obligation_status"] == "unsatisfied"
        assert result["standalone_price"] == "60000.00"

    def test_missing_name_fails(self, conn, env):
        c = _add_contract(conn, env)
        result = call_action(mod.add_performance_obligation, conn, ns(
            contract_id=c["id"], company_id=env["company_id"],
            name=None, standalone_price="1000",
            recognition_method="over_time", recognition_basis="time",
        ))
        assert is_error(result)

    def test_invalid_recognition_method_fails(self, conn, env):
        c = _add_contract(conn, env)
        result = call_action(mod.add_performance_obligation, conn, ns(
            contract_id=c["id"], company_id=env["company_id"],
            name="Test", standalone_price="1000",
            recognition_method="invalid", recognition_basis="time",
        ))
        assert is_error(result)


class TestListPerformanceObligations:
    def test_list(self, conn, env):
        c = _add_contract(conn, env)
        _add_obligation(conn, env, c["id"])
        result = call_action(mod.list_performance_obligations, conn, ns(
            contract_id=c["id"], company_id=None,
            obligation_status=None, limit=50, offset=0,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


class TestSatisfyPerformanceObligation:
    def test_full_satisfy(self, conn, env):
        c = _add_contract(conn, env)
        ob = _add_obligation(conn, env, c["id"])
        result = call_action(mod.satisfy_performance_obligation, conn, ns(
            id=ob["id"], pct_complete="100",
        ))
        assert is_ok(result)
        assert result["obligation_status"] == "satisfied"
        assert result["pct_complete"] == "100"

    def test_partial_satisfy(self, conn, env):
        c = _add_contract(conn, env)
        ob = _add_obligation(conn, env, c["id"])
        result = call_action(mod.satisfy_performance_obligation, conn, ns(
            id=ob["id"], pct_complete="50",
        ))
        assert is_ok(result)
        assert result["obligation_status"] == "partially_satisfied"

    def test_already_satisfied_fails(self, conn, env):
        c = _add_contract(conn, env)
        ob = _add_obligation(conn, env, c["id"])
        call_action(mod.satisfy_performance_obligation, conn, ns(
            id=ob["id"], pct_complete="100",
        ))
        result = call_action(mod.satisfy_performance_obligation, conn, ns(
            id=ob["id"], pct_complete="100",
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Variable Consideration
# ──────────────────────────────────────────────────────────────────────────────

class TestAddVariableConsideration:
    def test_basic_create(self, conn, env):
        c = _add_contract(conn, env)
        result = call_action(mod.add_variable_consideration, conn, ns(
            contract_id=c["id"], company_id=env["company_id"],
            description="Performance bonus", estimated_amount="5000.00",
            constraint_amount="3000.00", method="expected_value",
            probability="0.75",
        ))
        assert is_ok(result)
        assert result["description"] == "Performance bonus"
        assert result["estimated_amount"] == "5000.00"

    def test_missing_description_fails(self, conn, env):
        c = _add_contract(conn, env)
        result = call_action(mod.add_variable_consideration, conn, ns(
            contract_id=c["id"], company_id=env["company_id"],
            description=None, estimated_amount="1000",
            constraint_amount=None, method=None, probability=None,
        ))
        assert is_error(result)

    def test_invalid_method_fails(self, conn, env):
        c = _add_contract(conn, env)
        result = call_action(mod.add_variable_consideration, conn, ns(
            contract_id=c["id"], company_id=env["company_id"],
            description="Test", estimated_amount="1000",
            constraint_amount=None, method="invalid", probability=None,
        ))
        assert is_error(result)


class TestListVariableConsiderations:
    def test_list(self, conn, env):
        c = _add_contract(conn, env)
        call_action(mod.add_variable_consideration, conn, ns(
            contract_id=c["id"], company_id=env["company_id"],
            description="Bonus", estimated_amount="1000",
            constraint_amount=None, method=None, probability=None,
        ))
        result = call_action(mod.list_variable_considerations, conn, ns(
            contract_id=c["id"], company_id=None,
            limit=50, offset=0,
        ))
        assert is_ok(result)
        assert result["total_count"] >= 1


# ──────────────────────────────────────────────────────────────────────────────
# Contract Modification
# ──────────────────────────────────────────────────────────────────────────────

class TestModifyContract:
    def test_modify(self, conn, env):
        c = _add_contract(conn, env)
        result = call_action(mod.modify_contract, conn, ns(id=c["id"]))
        assert is_ok(result)
        assert result["contract_status"] == "modified"
        assert result["modification_count"] == 1

    def test_modify_completed_fails(self, conn, env):
        c = _add_contract(conn, env)
        call_action(mod.update_revenue_contract, conn, ns(
            id=c["id"], customer_name=None, contract_number=None,
            start_date=None, end_date=None, total_value=None,
            contract_status="completed",
        ))
        result = call_action(mod.modify_contract, conn, ns(id=c["id"]))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Revenue Schedule & Recognition
# ──────────────────────────────────────────────────────────────────────────────

class TestCalculateRevenueSchedule:
    def test_basic_schedule(self, conn, env):
        c = _add_contract(conn, env, start_date="2026-01-01", end_date="2026-12-31")
        ob = _add_obligation(conn, env, c["id"], standalone_price="12000.00")
        result = call_action(mod.calculate_revenue_schedule, conn, ns(
            obligation_id=ob["id"],
        ))
        assert is_ok(result)
        assert result["entries_created"] == 12
        assert Decimal(result["total_amount"]) == Decimal("12000.00")

    def test_missing_dates_fails(self, conn, env):
        c = _add_contract(conn, env, start_date=None, end_date=None)
        ob = _add_obligation(conn, env, c["id"])
        result = call_action(mod.calculate_revenue_schedule, conn, ns(
            obligation_id=ob["id"],
        ))
        assert is_error(result)


class TestGenerateRevenueEntries:
    def test_generate(self, conn, env):
        c = _add_contract(conn, env, start_date="2026-01-01", end_date="2026-06-30")
        ob = _add_obligation(conn, env, c["id"], standalone_price="6000.00")
        call_action(mod.calculate_revenue_schedule, conn, ns(
            obligation_id=ob["id"],
        ))
        result = call_action(mod.generate_revenue_entries, conn, ns(
            obligation_id=ob["id"],
        ))
        assert is_ok(result)
        assert result["recognized_count"] == 6
        assert Decimal(result["total_recognized"]) == Decimal("6000.00")

    def test_no_unrecognized_fails(self, conn, env):
        c = _add_contract(conn, env, start_date="2026-01-01", end_date="2026-03-31")
        ob = _add_obligation(conn, env, c["id"], standalone_price="3000.00")
        call_action(mod.calculate_revenue_schedule, conn, ns(
            obligation_id=ob["id"],
        ))
        call_action(mod.generate_revenue_entries, conn, ns(
            obligation_id=ob["id"],
        ))
        # All recognized now; second call should fail
        result = call_action(mod.generate_revenue_entries, conn, ns(
            obligation_id=ob["id"],
        ))
        assert is_error(result)


# ──────────────────────────────────────────────────────────────────────────────
# Reports
# ──────────────────────────────────────────────────────────────────────────────

class TestRevenueWaterfallReport:
    def test_report(self, conn, env):
        _add_contract(conn, env)
        result = call_action(mod.revenue_waterfall_report, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["total_contracts"] >= 1


class TestRevenueRecognitionSummary:
    def test_report(self, conn, env):
        c = _add_contract(conn, env, start_date="2026-01-01", end_date="2026-03-31")
        ob = _add_obligation(conn, env, c["id"], standalone_price="3000.00")
        call_action(mod.calculate_revenue_schedule, conn, ns(
            obligation_id=ob["id"],
        ))
        result = call_action(mod.revenue_recognition_summary, conn, ns(
            company_id=env["company_id"],
        ))
        assert is_ok(result)
        assert result["total_periods"] >= 1
