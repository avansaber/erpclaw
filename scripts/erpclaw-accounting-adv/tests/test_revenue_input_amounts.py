"""Revenue amount inputs refuse non-money text (m822).

Every amount a user gives the revenue-recognition actions is money (or a
percentage) and must be a finite number in range before anything is written.
Covers add-revenue-contract, update-revenue-contract,
add-performance-obligation, add-variable-consideration and
satisfy-performance-obligation.
"""
import pytest

from advacct_helpers import call_action, is_error, is_ok, load_db_query, ns

mod = load_db_query()

BAD_MONEY = ["NaN", "Infinity", "-Infinity", "abc", "-1.00", "10.001"]
GOOD_MONEY = ["0", "10", "10.5", "1234.56"]


def _add_contract(conn, env, total_value="100.00"):
    result = call_action(mod.add_revenue_contract, conn, ns(
        company_id=env["company_id"], customer_name="Acme Corp",
        total_value=total_value, contract_number="C-001",
        start_date="2026-01-01", end_date="2026-12-31",
    ))
    assert is_ok(result), result
    return result


def _add_obligation(conn, env, contract_id, standalone_price="100.00"):
    result = call_action(mod.add_performance_obligation, conn, ns(
        contract_id=contract_id, company_id=env["company_id"],
        name="Licence", standalone_price=standalone_price,
        recognition_method="over_time", recognition_basis="time",
    ))
    assert is_ok(result), result
    return result


def _count(conn, table):
    return conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]


def _audit_count(conn, action):
    return conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = ?", (action,)).fetchone()[0]


def _contract_row(conn, contract_id):
    return dict(conn.execute(
        "SELECT * FROM advacct_revenue_contract WHERE id = ?",
        (contract_id,)).fetchone())


def _obligation_row(conn, ob_id):
    return dict(conn.execute(
        "SELECT * FROM advacct_performance_obligation WHERE id = ?",
        (ob_id,)).fetchone())


class TestRefuseNonMoney:
    @pytest.mark.parametrize("value", BAD_MONEY)
    def test_add_contract_total_value(self, conn, env, value):
        before = _count(conn, "advacct_revenue_contract")
        before_audit = _audit_count(conn, "add-revenue-contract")
        result = call_action(mod.add_revenue_contract, conn, ns(
            company_id=env["company_id"], customer_name="Acme Corp",
            total_value=value, contract_number="C-001",
            start_date="2026-01-01", end_date="2026-12-31",
        ))
        assert is_error(result)
        assert result["message"] == "Invalid total-value: %s" % value
        assert _count(conn, "advacct_revenue_contract") == before
        assert _audit_count(conn, "add-revenue-contract") == before_audit

    @pytest.mark.parametrize("value", BAD_MONEY)
    def test_update_contract_total_value(self, conn, env, value):
        created = _add_contract(conn, env)
        before_row = _contract_row(conn, created["id"])
        before = _count(conn, "advacct_revenue_contract")
        before_audit = _audit_count(conn, "update-revenue-contract")
        result = call_action(mod.update_revenue_contract, conn, ns(
            id=created["id"], customer_name=None, contract_number=None,
            start_date=None, end_date=None, total_value=value,
            contract_status=None,
        ))
        assert is_error(result)
        assert result["message"] == "Invalid total-value: %s" % value
        assert _count(conn, "advacct_revenue_contract") == before
        assert _contract_row(conn, created["id"]) == before_row
        assert _audit_count(conn, "update-revenue-contract") == before_audit

    @pytest.mark.parametrize("value", BAD_MONEY)
    def test_add_obligation_standalone_price(self, conn, env, value):
        created = _add_contract(conn, env)
        before = _count(conn, "advacct_performance_obligation")
        before_audit = _audit_count(conn, "add-performance-obligation")
        result = call_action(mod.add_performance_obligation, conn, ns(
            contract_id=created["id"], company_id=env["company_id"],
            name="Licence", standalone_price=value,
            recognition_method="over_time", recognition_basis="time",
        ))
        assert is_error(result)
        assert result["message"] == "Invalid standalone-price: %s" % value
        assert _count(conn, "advacct_performance_obligation") == before
        assert _audit_count(conn, "add-performance-obligation") == before_audit

    @pytest.mark.parametrize("value", BAD_MONEY)
    def test_add_vc_estimated_amount(self, conn, env, value):
        created = _add_contract(conn, env)
        before = _count(conn, "advacct_variable_consideration")
        before_audit = _audit_count(conn, "add-variable-consideration")
        result = call_action(mod.add_variable_consideration, conn, ns(
            contract_id=created["id"], company_id=env["company_id"],
            description="Bonus", estimated_amount=value,
            constraint_amount="10.00", method="expected_value",
            probability="50",
        ))
        assert is_error(result)
        assert result["message"] == "Invalid estimated-amount: %s" % value
        assert _count(conn, "advacct_variable_consideration") == before
        assert _audit_count(conn, "add-variable-consideration") == before_audit

    @pytest.mark.parametrize("value", BAD_MONEY)
    def test_add_vc_constraint_amount(self, conn, env, value):
        created = _add_contract(conn, env)
        before = _count(conn, "advacct_variable_consideration")
        before_audit = _audit_count(conn, "add-variable-consideration")
        result = call_action(mod.add_variable_consideration, conn, ns(
            contract_id=created["id"], company_id=env["company_id"],
            description="Bonus", estimated_amount="100.00",
            constraint_amount=value, method="expected_value",
            probability="50",
        ))
        assert is_error(result)
        assert result["message"] == "Invalid constraint-amount: %s" % value
        assert _count(conn, "advacct_variable_consideration") == before
        assert _audit_count(conn, "add-variable-consideration") == before_audit


class TestAcceptMoney:
    @pytest.mark.parametrize("value", GOOD_MONEY)
    def test_add_contract_stores_exact(self, conn, env, value):
        result = call_action(mod.add_revenue_contract, conn, ns(
            company_id=env["company_id"], customer_name="Acme Corp",
            total_value=value, contract_number="C-001",
            start_date="2026-01-01", end_date="2026-12-31",
        ))
        assert is_ok(result), result
        assert _contract_row(conn, result["id"])["total_value"] == value

    @pytest.mark.parametrize("value", GOOD_MONEY)
    def test_update_contract_stores_exact(self, conn, env, value):
        created = _add_contract(conn, env)
        result = call_action(mod.update_revenue_contract, conn, ns(
            id=created["id"], customer_name=None, contract_number=None,
            start_date=None, end_date=None, total_value=value,
            contract_status=None,
        ))
        assert is_ok(result), result
        assert _contract_row(conn, created["id"])["total_value"] == value

    @pytest.mark.parametrize("value", GOOD_MONEY)
    def test_add_obligation_stores_exact(self, conn, env, value):
        created = _add_contract(conn, env)
        result = call_action(mod.add_performance_obligation, conn, ns(
            contract_id=created["id"], company_id=env["company_id"],
            name="Licence", standalone_price=value,
            recognition_method="over_time", recognition_basis="time",
        ))
        assert is_ok(result), result
        assert _obligation_row(conn, result["id"])["standalone_price"] == value

    @pytest.mark.parametrize("value", GOOD_MONEY)
    def test_add_vc_estimated_stores_exact(self, conn, env, value):
        created = _add_contract(conn, env)
        result = call_action(mod.add_variable_consideration, conn, ns(
            contract_id=created["id"], company_id=env["company_id"],
            description="Bonus", estimated_amount=value,
            constraint_amount="10.00", method="expected_value",
            probability="50",
        ))
        assert is_ok(result), result
        row = dict(conn.execute(
            "SELECT * FROM advacct_variable_consideration WHERE id = ?",
            (result["id"],)).fetchone())
        assert row["estimated_amount"] == value

    @pytest.mark.parametrize("value", GOOD_MONEY)
    def test_add_vc_constraint_stores_exact(self, conn, env, value):
        created = _add_contract(conn, env)
        result = call_action(mod.add_variable_consideration, conn, ns(
            contract_id=created["id"], company_id=env["company_id"],
            description="Bonus", estimated_amount="100.00",
            constraint_amount=value, method="expected_value",
            probability="50",
        ))
        assert is_ok(result), result
        row = dict(conn.execute(
            "SELECT * FROM advacct_variable_consideration WHERE id = ?",
            (result["id"],)).fetchone())
        assert row["constraint_amount"] == value


class TestSatisfyPctComplete:
    @pytest.mark.parametrize("value", ["NaN", "Infinity", "abc"])
    def test_non_finite_refused(self, conn, env, value):
        created = _add_contract(conn, env)
        ob = _add_obligation(conn, env, created["id"])
        before_row = _obligation_row(conn, ob["id"])
        before_audit = _audit_count(conn, "satisfy-performance-obligation")
        result = call_action(mod.satisfy_performance_obligation, conn, ns(
            id=ob["id"], pct_complete=value,
        ))
        assert is_error(result)
        assert result["message"] == "Invalid pct-complete: %s" % value
        assert _obligation_row(conn, ob["id"]) == before_row
        assert _audit_count(conn, "satisfy-performance-obligation") == before_audit

    @pytest.mark.parametrize("value", ["101", "-1"])
    def test_out_of_range_keeps_message(self, conn, env, value):
        created = _add_contract(conn, env)
        ob = _add_obligation(conn, env, created["id"])
        before_row = _obligation_row(conn, ob["id"])
        before_audit = _audit_count(conn, "satisfy-performance-obligation")
        result = call_action(mod.satisfy_performance_obligation, conn, ns(
            id=ob["id"], pct_complete=value,
        ))
        assert is_error(result)
        assert result["message"] == "pct-complete must be between 0 and 100"
        assert _obligation_row(conn, ob["id"]) == before_row
        assert _audit_count(conn, "satisfy-performance-obligation") == before_audit


class TestProbability:
    @pytest.mark.parametrize("value", ["NaN", "abc", "-1", "101"])
    def test_refused(self, conn, env, value):
        created = _add_contract(conn, env)
        before = _count(conn, "advacct_variable_consideration")
        before_audit = _audit_count(conn, "add-variable-consideration")
        result = call_action(mod.add_variable_consideration, conn, ns(
            contract_id=created["id"], company_id=env["company_id"],
            description="Bonus", estimated_amount="100.00",
            constraint_amount="10.00", method="expected_value",
            probability=value,
        ))
        assert is_error(result)
        assert result["message"] == "Invalid probability: %s" % value
        assert _count(conn, "advacct_variable_consideration") == before
        assert _audit_count(conn, "add-variable-consideration") == before_audit

    @pytest.mark.parametrize("value", ["50", "0.75"])
    def test_stored_as_given(self, conn, env, value):
        created = _add_contract(conn, env)
        result = call_action(mod.add_variable_consideration, conn, ns(
            contract_id=created["id"], company_id=env["company_id"],
            description="Bonus", estimated_amount="100.00",
            constraint_amount="10.00", method="expected_value",
            probability=value,
        ))
        assert is_ok(result), result
        row = dict(conn.execute(
            "SELECT * FROM advacct_variable_consideration WHERE id = ?",
            (result["id"],)).fetchone())
        assert row["probability"] == value
