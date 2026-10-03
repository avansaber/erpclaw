"""Regenerating or cancelling salary slips gives back garnishment amounts.

Covers m772: regeneration and cancellation revert exactly what the
discarded slips took, in the same transaction.
"""
import json
import uuid

from payroll_helpers import (
    call_action, ns, is_error, is_ok,
    build_payroll_env, load_db_query, seed_account,
)
from erpclaw_lib.db import get_connection

mod = load_db_query()


def _setup_payroll_ready(conn, env, extra_deduction=None):
    comp = call_action(mod.add_salary_component, conn, ns(
        name="Regen Base %s" % uuid.uuid4().hex[:6], component_type="earning",
        is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
        variable_based_on_taxable_salary=None, depends_on_payment_days=None,
        gl_account_id=None, description=None,
    ))
    assert is_ok(comp), comp
    comp_id = comp["salary_component_id"]
    details = [{"salary_component_id": comp_id, "amount": "5000"}]
    if extra_deduction is not None:
        details.append(extra_deduction)
    ss = call_action(mod.add_salary_structure, conn, ns(
        name="Regen Structure %s" % uuid.uuid4().hex[:6],
        company_id=env["company_id"],
        components=json.dumps(details), payroll_frequency=None,
    ))
    assert is_ok(ss), ss
    sa = call_action(mod.add_salary_assignment, conn, ns(
        employee_id=env["employee_id"], salary_structure_id=ss["salary_structure_id"],
        base_amount="5000.00", effective_from="2026-01-01", effective_to=None,
    ))
    assert is_ok(sa), sa
    fica = call_action(mod.update_fica_config, conn, ns(
        tax_year="2026", ss_wage_base="168600", ss_employee_rate="6.2",
        ss_employer_rate="6.2", medicare_employee_rate="1.45",
        medicare_employer_rate="1.45",
        additional_medicare_threshold="200000", additional_medicare_rate="0.9",
    ))
    assert is_ok(fica), fica
    seed_account(conn, env["company_id"], "Garnishments Payable",
                 root_type="liability", account_type=None)
    return {"component_id": comp_id, "structure_id": ss["salary_structure_id"]}


def _add_garnishment(conn, employee_id, order, creditor, total, amount="200",
                     gtype="child_support", pct=False):
    res = call_action(mod.add_garnishment, conn, ns(
        employee_id=employee_id, order_number=order, creditor_name=creditor,
        garnishment_type=gtype, amount_or_percentage=amount,
        is_percentage=pct, total_owed=total, start_date="2026-01-01",
        end_date=None,
    ))
    assert is_ok(res), res
    return res["garnishment_id"]


def _create_run(conn, company_id, start="2026-01-01", end="2026-01-31"):
    res = call_action(mod.create_payroll_run, conn, ns(
        company_id=company_id, period_start=start, period_end=end,
        department_id=None, payroll_frequency="monthly",
    ))
    assert is_ok(res), res
    return res["payroll_run_id"]


def _generate(conn, run_id):
    return call_action(mod.generate_salary_slips, conn, ns(payroll_run_id=run_id))


def _garn_row(conn, gid):
    return conn.execute(
        "SELECT * FROM wage_garnishment WHERE id = ?", (gid,)).fetchone()


def _run_slips(conn, run_id, status=None):
    if status is None:
        return conn.execute(
            "SELECT * FROM salary_slip WHERE payroll_run_id = ?", (run_id,)).fetchall()
    return conn.execute(
        "SELECT * FROM salary_slip WHERE payroll_run_id = ? AND status = ?",
        (run_id, status)).fetchall()


def _garn_details(conn, slip_id):
    return conn.execute(
        """SELECT ssd.amount AS amount, sc.name AS name
           FROM salary_slip_detail ssd
           JOIN salary_component sc ON sc.id = ssd.salary_component_id
           WHERE ssd.salary_slip_id = ?
             AND sc.component_type = 'deduction'
             AND sc.is_statutory = 1
             AND sc.name LIKE 'Garnishment - %%'""",
        (slip_id,)).fetchall()


def _garnish_payable_legs(conn, run_id):
    return [dict(r) for r in conn.execute(
        """SELECT e.debit AS debit, e.credit AS credit,
                  e.party_type AS party_type, e.party_id AS party_id
           FROM gl_entry e JOIN account a ON a.id = e.account_id
           WHERE e.voucher_id = ? AND a.name = 'Garnishments Payable'""",
        (run_id,)).fetchall()]


def _fresh_rows(table):
    conn2 = get_connection()
    try:
        return [dict(r) for r in conn2.execute(
            'SELECT * FROM "%s" ORDER BY id' % table).fetchall()]
    finally:
        conn2.close()


class TestRegenerateKeepsSingleAccrual:
    def test_three_generates_leave_200(self, conn, env):
        _setup_payroll_ready(conn, env)
        gid = _add_garnishment(conn, env["employee_id"], "CS-RG-001",
                               "Regen Creditor", "600.00")
        run_id = _create_run(conn, env["company_id"])
        for _ in range(3):
            res = _generate(conn, run_id)
            assert is_ok(res), res
            row = _garn_row(conn, gid)
            assert row["cumulative_paid"] == "200.00"
            assert row["status"] == "active"
            slips = _run_slips(conn, run_id)
            assert len(slips) == 1
            details = _garn_details(conn, slips[0]["id"])
            assert len(details) == 1
            assert details[0]["amount"] == "200.00"


class TestRegenerateRecompletes:
    def test_total_owed_200_recompletes(self, conn, env):
        _setup_payroll_ready(conn, env)
        gid = _add_garnishment(conn, env["employee_id"], "CS-RG-002",
                               "Regen Creditor", "200.00")
        run_id = _create_run(conn, env["company_id"])
        for _ in range(2):
            res = _generate(conn, run_id)
            assert is_ok(res), res
            row = _garn_row(conn, gid)
            assert row["cumulative_paid"] == "200.00"
            assert row["status"] == "completed"


class TestCancelGivesBack:
    # The run credits the garnishment to Garnishments Payable (m803).
    def test_submit_then_cancel(self, conn, env):
        _setup_payroll_ready(conn, env)
        gid = _add_garnishment(conn, env["employee_id"], "CS-RG-003",
                               "Regen Creditor", "600.00", amount="0.50")
        run_id = _create_run(conn, env["company_id"])
        assert is_ok(_generate(conn, run_id))
        assert _garn_row(conn, gid)["cumulative_paid"] == "0.50"
        sub = call_action(mod.submit_payroll_run, conn, ns(
            payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
        assert is_ok(sub), sub
        legs = _garnish_payable_legs(conn, run_id)
        assert len(legs) == 1
        assert legs[0]["credit"] == "0.50"
        assert legs[0]["party_type"] is None
        assert legs[0]["party_id"] is None
        res = call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=run_id))
        assert is_ok(res), res
        assert res["garnishments_reverted"] == 1
        row = _garn_row(conn, gid)
        assert row["cumulative_paid"] == "0.00"
        assert row["status"] == "active"
        legs = _garnish_payable_legs(conn, run_id)
        assert len(legs) == 2
        assert sum(1 for leg in legs if leg["credit"] == "0.50") == 1
        assert sum(1 for leg in legs if leg["debit"] == "0.50") == 1


class TestPausedUntouched:
    def test_paused_not_touched(self, conn, env):
        _setup_payroll_ready(conn, env)
        gid = _add_garnishment(conn, env["employee_id"], "CS-RG-004",
                               "Paused Creditor", "600.00")
        upd = call_action(mod.update_garnishment, conn, ns(
            garnishment_id=gid, status="paused",
            amount_or_percentage=None, total_owed=None, end_date=None))
        assert is_ok(upd), upd
        run_id = _create_run(conn, env["company_id"])
        assert is_ok(_generate(conn, run_id))
        row = _garn_row(conn, gid)
        assert row["status"] == "paused"
        assert row["cumulative_paid"] == "0"
        slips = _run_slips(conn, run_id)
        assert len(slips) == 1
        assert len(_garn_details(conn, slips[0]["id"])) == 0
        sub = call_action(mod.submit_payroll_run, conn, ns(
            payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
        assert is_ok(sub), sub
        res = call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=run_id))
        assert is_ok(res), res
        row = _garn_row(conn, gid)
        assert row["status"] == "paused"
        assert row["cumulative_paid"] == "0"


class TestUnknownComponentRefuses:
    def test_regeneration_refused_and_unchanged(self, conn, env, db_path):
        _setup_payroll_ready(conn, env)
        gid = _add_garnishment(conn, env["employee_id"], "CS-RG-005",
                               "Real Creditor", "600.00")
        run_id = _create_run(conn, env["company_id"])
        assert is_ok(_generate(conn, run_id))
        slips = _run_slips(conn, run_id)
        assert len(slips) == 1
        slip_id = slips[0]["id"]
        ghost = call_action(mod.add_salary_component, conn, ns(
            name="Garnishment - Ghost Creditor", component_type="deduction",
            is_tax_applicable=None, is_statutory="1", is_pre_tax=None,
            variable_based_on_taxable_salary=None, depends_on_payment_days=None,
            gl_account_id=None, description=None,
        ))
        assert is_ok(ghost), ghost
        conn.execute(
            """INSERT INTO salary_slip_detail
               (id, salary_slip_id, salary_component_id, component_type, amount, year_to_date)
               VALUES (?, ?, ?, 'deduction', '50.00', '0')""",
            (str(uuid.uuid4()), slip_id, ghost["salary_component_id"]),
        )
        conn.commit()
        slips_before = _fresh_rows("salary_slip")
        det_before = [dict(r) for r in conn.execute(
            "SELECT * FROM salary_slip_detail ORDER BY id").fetchall()]
        garn_before = [dict(r) for r in conn.execute(
            "SELECT * FROM wage_garnishment ORDER BY id").fetchall()]
        res = _generate(conn, run_id)
        assert is_error(res), res
        assert "matches 0 garnishment orders" in res["message"]
        assert "nothing was changed" in res["message"]
        slips_after = _fresh_rows("salary_slip")
        assert slips_after == slips_before
        conn2 = get_connection()
        try:
            det_after = [dict(r) for r in conn2.execute(
                "SELECT * FROM salary_slip_detail ORDER BY id").fetchall()]
            garn_after = [dict(r) for r in conn2.execute(
                "SELECT * FROM wage_garnishment ORDER BY id").fetchall()]
        finally:
            conn2.close()
        assert det_after == det_before
        assert garn_after == garn_before


class TestManualCompleteStaysCompleted:
    def test_with_total_owed(self, conn, env):
        _setup_payroll_ready(conn, env)
        gid = _add_garnishment(conn, env["employee_id"], "CS-RG-006",
                               "Manual Creditor", "600.00")
        run_id = _create_run(conn, env["company_id"])
        assert is_ok(_generate(conn, run_id))
        upd = call_action(mod.update_garnishment, conn, ns(
            garnishment_id=gid, status="completed",
            amount_or_percentage=None, total_owed=None, end_date=None))
        assert is_ok(upd), upd
        res = _generate(conn, run_id)
        assert is_ok(res), res
        row = _garn_row(conn, gid)
        assert row["status"] == "completed"
        assert row["cumulative_paid"] == "0.00"
        sub = call_action(mod.submit_payroll_run, conn, ns(
            payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
        assert is_ok(sub), sub
        cancel = call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=run_id))
        assert is_ok(cancel), cancel
        row = _garn_row(conn, gid)
        assert row["status"] == "completed"

    def test_without_total_owed(self, conn, env):
        _setup_payroll_ready(conn, env)
        gid = _add_garnishment(conn, env["employee_id"], "CS-RG-007",
                               "NoTotal Creditor", None)
        run_id = _create_run(conn, env["company_id"])
        assert is_ok(_generate(conn, run_id))
        upd = call_action(mod.update_garnishment, conn, ns(
            garnishment_id=gid, status="completed",
            amount_or_percentage=None, total_owed=None, end_date=None))
        assert is_ok(upd), upd
        res = _generate(conn, run_id)
        assert is_ok(res), res
        row = _garn_row(conn, gid)
        assert row["status"] == "completed"
        assert row["cumulative_paid"] == "0.00"
        sub = call_action(mod.submit_payroll_run, conn, ns(
            payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
        assert is_ok(sub), sub
        cancel = call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=run_id))
        assert is_ok(cancel), cancel
        row = _garn_row(conn, gid)
        assert row["status"] == "completed"


class TestActiveThenPausedBeforeCancel:
    # The run credits the garnishment to Garnishments Payable (m803).
    def test_paused_keeps_status_but_gives_back(self, conn, env):
        _setup_payroll_ready(conn, env)
        gid = _add_garnishment(conn, env["employee_id"], "CS-RG-008",
                               "Flip Creditor", "600.00", amount="0.50")
        run_id = _create_run(conn, env["company_id"])
        assert is_ok(_generate(conn, run_id))
        sub = call_action(mod.submit_payroll_run, conn, ns(
            payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
        assert is_ok(sub), sub
        legs = _garnish_payable_legs(conn, run_id)
        assert len(legs) == 1
        assert legs[0]["credit"] == "0.50"
        assert legs[0]["party_type"] is None
        assert legs[0]["party_id"] is None
        upd = call_action(mod.update_garnishment, conn, ns(
            garnishment_id=gid, status="paused",
            amount_or_percentage=None, total_owed=None, end_date=None))
        assert is_ok(upd), upd
        res = call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=run_id))
        assert is_ok(res), res
        row = _garn_row(conn, gid)
        assert row["cumulative_paid"] == "0.00"
        assert row["status"] == "paused"
        legs = _garnish_payable_legs(conn, run_id)
        assert len(legs) == 2
        assert sum(1 for leg in legs if leg["debit"] == "0.50") == 1


class TestStabilitySecondOrder:
    def test_cancel_after_second_order(self, conn, env):
        _setup_payroll_ready(conn, env)
        gid1 = _add_garnishment(conn, env["employee_id"], "STAB-001",
                                "Stability Creditor", "600.00", amount="0.50")
        run_id = _create_run(conn, env["company_id"])
        assert is_ok(_generate(conn, run_id))
        sub = call_action(mod.submit_payroll_run, conn, ns(
            payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))
        assert is_ok(sub), sub
        legs = _garnish_payable_legs(conn, run_id)
        assert len(legs) == 1
        assert legs[0]["credit"] == "0.50"
        assert legs[0]["party_type"] is None
        assert legs[0]["party_id"] is None
        gid2 = _add_garnishment(conn, env["employee_id"], "STAB-002",
                                "Stability Creditor", "900.00", amount="0.75")
        assert gid2 != gid1
        # Force the second order strictly after the slip: CURRENT_TIMESTAMP is
        # second-precision, so a fast test would otherwise tie and stay ambiguous.
        slip_id = _run_slips(conn, run_id)[0]["id"]
        conn.execute(
            "UPDATE wage_garnishment SET created_at = "
            "datetime((SELECT created_at FROM salary_slip WHERE id = ?), '+1 day') "
            "WHERE id = ?",
            (slip_id, gid2),
        )
        conn.commit()
        res = call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=run_id))
        assert is_ok(res), res
        assert _garn_row(conn, gid1)["cumulative_paid"] == "0.00"
        legs = _garnish_payable_legs(conn, run_id)
        assert len(legs) == 2
        assert sum(1 for leg in legs if leg["debit"] == "0.50") == 1


class TestTwoOrdersSameCreditor:
    def test_bracket_names_no_order_number(self, conn, env):
        _setup_payroll_ready(conn, env)
        gid1 = _add_garnishment(conn, env["employee_id"], "DUP-ORDER-001",
                                "Dup Creditor", "600.00")
        gid2 = _add_garnishment(conn, env["employee_id"], "DUP-ORDER-002",
                                "Dup Creditor", "600.00",
                                gtype="tax_levy")
        run_id = _create_run(conn, env["company_id"])
        assert is_ok(_generate(conn, run_id))
        slips = _run_slips(conn, run_id)
        assert len(slips) == 1
        rows = conn.execute(
            """SELECT sc.name AS name, ssd.amount AS amount
               FROM salary_slip_detail ssd
               JOIN salary_component sc ON sc.id = ssd.salary_component_id
               WHERE ssd.salary_slip_id = ?
                 AND sc.name LIKE 'Garnishment - %%'
               ORDER BY sc.name""",
            (slips[0]["id"],)).fetchall()
        names = sorted([r["name"] for r in rows])
        assert names == sorted([
            "Garnishment - Dup Creditor [%s]" % gid1[:8],
            "Garnishment - Dup Creditor [%s]" % gid2[:8],
        ])
        for name in names:
            assert "DUP-ORDER-001" not in name
            assert "DUP-ORDER-002" not in name


class TestNonStatutoryIgnored:
    def test_manual_component_ignored(self, conn, env):
        manual = call_action(mod.add_salary_component, conn, ns(
            name="Garnishment - Manual", component_type="deduction",
            is_tax_applicable=None, is_statutory="0", is_pre_tax=None,
            variable_based_on_taxable_salary=None, depends_on_payment_days=None,
            gl_account_id=None, description=None,
        ))
        assert is_ok(manual), manual
        _setup_payroll_ready(conn, env, extra_deduction={
            "salary_component_id": manual["salary_component_id"], "amount": "50"})
        gid = _add_garnishment(conn, env["employee_id"], "CS-RG-009",
                               "Real Creditor", "600.00")
        run_id = _create_run(conn, env["company_id"])
        assert is_ok(_generate(conn, run_id))
        res = _generate(conn, run_id)
        assert is_ok(res), res
        row = _garn_row(conn, gid)
        assert row["cumulative_paid"] == "200.00"
        assert row["status"] == "active"
