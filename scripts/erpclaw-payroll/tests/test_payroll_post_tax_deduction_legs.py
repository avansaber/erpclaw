"""Post-tax deductions post to a liability account (m803).

A submitted payroll run must carry a credit for every amount withheld from
pay, including post-tax deductions that are not one of the four statutory
taxes (a wage garnishment in particular). Each such deduction is credited
to a liability account of the run's company; a run that cannot resolve one
is refused before anything is written.
"""
import json
import uuid
from collections import defaultdict
from decimal import Decimal

from payroll_helpers import (
    build_payroll_env, call_action, is_error, is_ok, load_db_query, ns,
    seed_account, seed_company,
)

mod = load_db_query()


# ──────────────────────────────────────────────────────────────────────────────
# Builders
# ──────────────────────────────────────────────────────────────────────────────

def _setup_earning(conn, env, extra_details=None):
    comp = call_action(mod.add_salary_component, conn, ns(
        name="M803 Base %s" % uuid.uuid4().hex[:6], component_type="earning",
        is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
        variable_based_on_taxable_salary=None, depends_on_payment_days=None,
        gl_account_id=None, description=None,
    ))
    assert is_ok(comp), comp
    details = [{"salary_component_id": comp["salary_component_id"], "amount": "5000"}]
    if extra_details:
        details.extend(extra_details)
    ss = call_action(mod.add_salary_structure, conn, ns(
        name="M803 Structure %s" % uuid.uuid4().hex[:6],
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
    return comp["salary_component_id"]


def _add_deduction_component(conn, name, gl_account_id=None):
    comp = call_action(mod.add_salary_component, conn, ns(
        name=name, component_type="deduction",
        is_tax_applicable=None, is_statutory="0", is_pre_tax="0",
        variable_based_on_taxable_salary=None, depends_on_payment_days=None,
        gl_account_id=gl_account_id, description=None,
    ))
    assert is_ok(comp), comp
    return comp["salary_component_id"]


def _seed_garnishments_payable(conn, env):
    return seed_account(conn, env["company_id"], "Garnishments Payable",
                        root_type="liability", account_type=None)


def _add_garnishment(conn, employee_id, creditor, amount, total="2000.00",
                     order=None, gtype="child_support"):
    res = call_action(mod.add_garnishment, conn, ns(
        employee_id=employee_id,
        order_number=order or ("CS-M803-%s" % uuid.uuid4().hex[:6]),
        creditor_name=creditor, garnishment_type=gtype,
        amount_or_percentage=amount, is_percentage=False,
        total_owed=total, start_date="2026-01-01", end_date=None,
    ))
    assert is_ok(res), res
    return res["garnishment_id"]


def _create_run(conn, company_id):
    res = call_action(mod.create_payroll_run, conn, ns(
        company_id=company_id, period_start="2026-01-01", period_end="2026-01-31",
        department_id=None, payroll_frequency="monthly",
    ))
    assert is_ok(res), res
    return res["payroll_run_id"]


def _generate(conn, run_id):
    res = call_action(mod.generate_salary_slips, conn, ns(payroll_run_id=run_id))
    assert is_ok(res), res
    return res


def _submit(conn, env, run_id):
    return call_action(mod.submit_payroll_run, conn, ns(
        payroll_run_id=run_id, cost_center_id=env["cost_center_id"]))


def _ledger(conn, run_id):
    rows = conn.execute(
        """SELECT a.name AS name, e.debit AS debit, e.credit AS credit,
                  e.party_type AS party_type, e.party_id AS party_id,
                  e.is_cancelled AS is_cancelled
           FROM gl_entry e JOIN account a ON a.id = e.account_id
           WHERE e.voucher_id = ?
           ORDER BY a.name""",
        (run_id,)).fetchall()
    return [dict(r) for r in rows]


def _head(conn, company_id):
    row = conn.execute(
        "SELECT * FROM gl_chain_head WHERE company_id = ?",
        (company_id,)).fetchone()
    return dict(row) if row else None


def _cumulative(conn, gid):
    return conn.execute(
        "SELECT cumulative_paid FROM wage_garnishment WHERE id = ?",
        (gid,)).fetchone()["cumulative_paid"]


def _assert_refused(conn, env, run_id, expected_message, garnishment_id=None):
    head_before = _head(conn, env["company_id"])
    cumulative_before = _cumulative(conn, garnishment_id) if garnishment_id else None
    res = _submit(conn, env, run_id)
    assert is_error(res), res
    assert res["message"] == expected_message
    assert conn.execute(
        "SELECT status FROM payroll_run WHERE id = ?", (run_id,)).fetchone()["status"] == "draft"
    slips = conn.execute(
        "SELECT status FROM salary_slip WHERE payroll_run_id = ?",
        (run_id,)).fetchall()
    assert len(slips) >= 1
    assert all(s["status"] == "draft" for s in slips)
    assert conn.execute(
        "SELECT COUNT(*) FROM gl_entry WHERE voucher_id = ?",
        (run_id,)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM audit_log WHERE action = 'submit-payroll-run'"
        " AND entity_id = ?",
        (run_id,)).fetchone()[0] == 0
    if garnishment_id:
        assert _cumulative(conn, garnishment_id) == cumulative_before
    # Read on the action's own connection with no rollback(): the head take
    # was rolled back by the refusal, so the row is unchanged.
    assert _head(conn, env["company_id"]) == head_before


# ──────────────────────────────────────────────────────────────────────────────
# Success legs
# ──────────────────────────────────────────────────────────────────────────────

class TestGarnishmentPosts:
    def test_garnishment_credited_to_liability(self, conn, env):
        _seed_garnishments_payable(conn, env)
        _setup_earning(conn, env)
        _add_garnishment(conn, env["employee_id"], "Acme Collections", "200")
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        res = _submit(conn, env, run_id)
        assert is_ok(res), res
        rows = _ledger(conn, run_id)
        assert {(r["name"], r["debit"], r["credit"]) for r in rows} == {
            ("Salary Expense", "5000.00", "0.00"),
            ("Employer Tax Expense", "382.50", "0.00"),
            ("Payroll Payable", "0.00", "4417.50"),
            ("Social Security Payable", "0.00", "620.00"),
            ("Medicare Payable", "0.00", "145.00"),
            ("Garnishments Payable", "0.00", "200.00"),
        }
        garn = [r for r in rows if r["name"] == "Garnishments Payable"]
        assert len(garn) == 1
        assert garn[0]["party_type"] is None
        assert garn[0]["party_id"] is None
        pay = [r for r in rows if r["name"] == "Payroll Payable"]
        assert len(pay) == 1
        assert pay[0]["party_type"] == "employee"
        assert pay[0]["party_id"] == env["employee_id"]
        total_debit = sum(Decimal(r["debit"]) for r in rows)
        total_credit = sum(Decimal(r["credit"]) for r in rows)
        assert total_debit == Decimal("5382.50")
        assert total_credit == Decimal("5382.50")


class TestComponentAccountWins:
    def test_component_gl_account_used(self, conn, env):
        _seed_garnishments_payable(conn, env)
        court_orders = seed_account(conn, env["company_id"], "Court Orders Payable",
                                    root_type="liability", account_type=None)
        _setup_earning(conn, env)
        _add_garnishment(conn, env["employee_id"], "Court Creditor", "200")
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        conn.execute(
            "UPDATE salary_component SET gl_account_id = ? WHERE name = ?",
            (court_orders, "Garnishment - Court Creditor"))
        conn.commit()
        res = _submit(conn, env, run_id)
        assert is_ok(res), res
        rows = _ledger(conn, run_id)
        court = [r for r in rows if r["name"] == "Court Orders Payable"]
        assert len(court) == 1
        assert court[0]["credit"] == "200.00"
        assert court[0]["party_type"] is None
        assert [r for r in rows if r["name"] == "Garnishments Payable"] == []


class TestForeignComponentAccountFallsBack:
    def test_other_company_account_ignored(self, conn, env):
        _seed_garnishments_payable(conn, env)
        other_company = seed_company(conn, name="Foreign Co", abbr="FC")
        foreign_acct = seed_account(conn, other_company, "Foreign Orders Payable",
                                    root_type="liability", account_type=None)
        _setup_earning(conn, env)
        _add_garnishment(conn, env["employee_id"], "Court Creditor", "200")
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        conn.execute(
            "UPDATE salary_component SET gl_account_id = ? WHERE name = ?",
            (foreign_acct, "Garnishment - Court Creditor"))
        conn.commit()
        res = _submit(conn, env, run_id)
        assert is_ok(res), res
        rows = _ledger(conn, run_id)
        garn = [r for r in rows if r["name"] == "Garnishments Payable"]
        assert len(garn) == 1
        assert garn[0]["credit"] == "200.00"


class TestOtherPostTaxDeduction:
    def test_union_dues_credited(self, conn, env):
        dues_payable = seed_account(conn, env["company_id"], "Union Dues Payable",
                                    root_type="liability", account_type=None)
        dues = _add_deduction_component(conn, "Union Dues", dues_payable)
        _setup_earning(conn, env, extra_details=[
            {"salary_component_id": dues, "amount": "15.00"}])
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        res = _submit(conn, env, run_id)
        assert is_ok(res), res
        rows = _ledger(conn, run_id)
        assert {(r["name"], r["debit"], r["credit"]) for r in rows} == {
            ("Salary Expense", "5000.00", "0.00"),
            ("Employer Tax Expense", "382.50", "0.00"),
            ("Payroll Payable", "0.00", "4602.50"),
            ("Social Security Payable", "0.00", "620.00"),
            ("Medicare Payable", "0.00", "145.00"),
            ("Union Dues Payable", "0.00", "15.00"),
        }


class TestTwoComponentsOneAccount:
    def test_single_grouped_credit(self, conn, env):
        dues_payable = seed_account(conn, env["company_id"], "Union Dues Payable",
                                    root_type="liability", account_type=None)
        dues = _add_deduction_component(conn, "Union Dues", dues_payable)
        parking = _add_deduction_component(conn, "Parking", dues_payable)
        _setup_earning(conn, env, extra_details=[
            {"salary_component_id": dues, "amount": "15.00"},
            {"salary_component_id": parking, "amount": "10.00"}])
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        res = _submit(conn, env, run_id)
        assert is_ok(res), res
        rows = _ledger(conn, run_id)
        dues_rows = [r for r in rows if r["name"] == "Union Dues Payable"]
        assert len(dues_rows) == 1
        assert dues_rows[0]["credit"] == "25.00"
        pay = [r for r in rows if r["name"] == "Payroll Payable"]
        assert len(pay) == 1
        assert pay[0]["credit"] == "4592.50"


# ──────────────────────────────────────────────────────────────────────────────
# Refusals
# ──────────────────────────────────────────────────────────────────────────────

class TestRefusals:
    def test_no_garnish_account(self, conn, env):
        _setup_earning(conn, env)
        gid = _add_garnishment(conn, env["employee_id"], "Acme Collections", "200")
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        _assert_refused(
            conn, env, run_id,
            "Cannot post payroll: no liability account for 'Garnishment - Acme Collections'"
            " (needed for 200.00; set the component's GL account,"
            " or create a liability account with 'Garnishment' in its name)."
            " Nothing was posted.",
            garnishment_id=gid)

    def test_union_dues_no_account(self, conn, env):
        dues = _add_deduction_component(conn, "Union Dues")
        _setup_earning(conn, env, extra_details=[
            {"salary_component_id": dues, "amount": "15.00"}])
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        _assert_refused(
            conn, env, run_id,
            "Cannot post payroll: no liability account for 'Union Dues'"
            " (needed for 15.00; set the component's GL account)."
            " Nothing was posted.")

    def test_union_dues_foreign_account(self, conn, env):
        other_company = seed_company(conn, name="Foreign Co", abbr="FC")
        foreign_acct = seed_account(conn, other_company, "Foreign Dues Payable",
                                    root_type="liability", account_type=None)
        dues = _add_deduction_component(conn, "Union Dues", foreign_acct)
        _setup_earning(conn, env, extra_details=[
            {"salary_component_id": dues, "amount": "15.00"}])
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        _assert_refused(
            conn, env, run_id,
            "Cannot post payroll: GL account 'Foreign Dues Payable' of 'Union Dues'"
            " is not a usable liability account of this company"
            " (needed for 15.00; set the component's GL account)."
            " Nothing was posted.")

    def test_union_dues_expense_account(self, conn, env):
        expense_acct = seed_account(conn, env["company_id"], "Dues Expense",
                                    root_type="expense", account_type=None)
        dues = _add_deduction_component(conn, "Union Dues", expense_acct)
        _setup_earning(conn, env, extra_details=[
            {"salary_component_id": dues, "amount": "15.00"}])
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        _assert_refused(
            conn, env, run_id,
            "Cannot post payroll: GL account 'Dues Expense' of 'Union Dues'"
            " is not a usable liability account of this company"
            " (needed for 15.00; set the component's GL account)."
            " Nothing was posted.")

    def test_union_dues_payable_type_account(self, conn, env):
        dues = _add_deduction_component(conn, "Union Dues", env["payroll_payable"])
        _setup_earning(conn, env, extra_details=[
            {"salary_component_id": dues, "amount": "15.00"}])
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        _assert_refused(
            conn, env, run_id,
            "Cannot post payroll: GL account 'Payroll Payable' of 'Union Dues'"
            " is not a usable liability account of this company"
            " (needed for 15.00; set the component's GL account)."
            " Nothing was posted.")

    def test_combined_items_ordered(self, conn, env):
        dues = _add_deduction_component(conn, "Union Dues")
        _setup_earning(conn, env, extra_details=[
            {"salary_component_id": dues, "amount": "15.00"}])
        gid = _add_garnishment(conn, env["employee_id"], "Acme Collections", "200")
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        _assert_refused(
            conn, env, run_id,
            "Cannot post payroll: no liability account for 'Garnishment - Acme Collections'"
            " (needed for 200.00; set the component's GL account,"
            " or create a liability account with 'Garnishment' in its name),"
            " no liability account for 'Union Dues'"
            " (needed for 15.00; set the component's GL account)."
            " Nothing was posted.",
            garnishment_id=gid)


# ──────────────────────────────────────────────────────────────────────────────
# Decoys: unusable %garnish% accounts do not satisfy the fallback
# ──────────────────────────────────────────────────────────────────────────────

def _decoy_refused(conn, env):
    _setup_earning(conn, env)
    gid = _add_garnishment(conn, env["employee_id"], "Acme Collections", "200")
    run_id = _create_run(conn, env["company_id"])
    _generate(conn, run_id)
    _assert_refused(
        conn, env, run_id,
        "Cannot post payroll: no liability account for 'Garnishment - Acme Collections'"
        " (needed for 200.00; set the component's GL account,"
        " or create a liability account with 'Garnishment' in its name)."
        " Nothing was posted.",
        garnishment_id=gid)


class TestDecoys:
    def test_second_company_only(self, conn, env):
        other_company = seed_company(conn, name="Foreign Co", abbr="FC")
        seed_account(conn, other_company, "Garnishments Payable",
                     root_type="liability", account_type=None)
        _decoy_refused(conn, env)

    def test_disabled(self, conn, env):
        seed_account(conn, env["company_id"], "Garnish Disabled",
                     root_type="liability", account_type=None, disabled=1)
        _decoy_refused(conn, env)

    def test_group(self, conn, env):
        seed_account(conn, env["company_id"], "Garnish Group",
                     root_type="liability", account_type=None, is_group=1)
        _decoy_refused(conn, env)

    def test_frozen(self, conn, env):
        aid = seed_account(conn, env["company_id"], "Garnish Frozen",
                           root_type="liability", account_type=None)
        conn.execute("UPDATE account SET is_frozen = 1 WHERE id = ?", (aid,))
        conn.commit()
        _decoy_refused(conn, env)

    def test_payable_type(self, conn, env):
        seed_account(conn, env["company_id"], "Garnish Holding",
                     root_type="liability", account_type="payable")
        _decoy_refused(conn, env)


# ──────────────────────────────────────────────────────────────────────────────
# Small amount
# ──────────────────────────────────────────────────────────────────────────────

class TestSmallAmount:
    def test_small_garnishment_posts(self, conn, env):
        _seed_garnishments_payable(conn, env)
        _setup_earning(conn, env)
        _add_garnishment(conn, env["employee_id"], "Acme Collections", "0.50")
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        res = _submit(conn, env, run_id)
        assert is_ok(res), res
        rows = _ledger(conn, run_id)
        garn = [r for r in rows if r["name"] == "Garnishments Payable"]
        assert len(garn) == 1
        assert garn[0]["credit"] == "0.50"
        pay = [r for r in rows if r["name"] == "Payroll Payable"]
        assert len(pay) == 1
        assert pay[0]["credit"] == "4617.00"

    def test_small_garnishment_refused_without_account(self, conn, env):
        _setup_earning(conn, env)
        gid = _add_garnishment(conn, env["employee_id"], "Acme Collections", "0.50")
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        _assert_refused(
            conn, env, run_id,
            "Cannot post payroll: no liability account for 'Garnishment - Acme Collections'"
            " (needed for 0.50; set the component's GL account,"
            " or create a liability account with 'Garnishment' in its name)."
            " Nothing was posted.",
            garnishment_id=gid)


# ──────────────────────────────────────────────────────────────────────────────
# Cancel reverses the new legs
# ──────────────────────────────────────────────────────────────────────────────

class TestCancel:
    def test_cancel_reverses_garnishment_leg(self, conn, env):
        _seed_garnishments_payable(conn, env)
        _setup_earning(conn, env)
        gid = _add_garnishment(conn, env["employee_id"], "Acme Collections", "200")
        run_id = _create_run(conn, env["company_id"])
        _generate(conn, run_id)
        assert is_ok(_submit(conn, env, run_id)), run_id
        res = call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=run_id))
        assert is_ok(res), res
        rows = _ledger(conn, run_id)
        assert len(rows) == 12
        assert all(r["is_cancelled"] == 1 for r in rows)
        nets = defaultdict(Decimal)
        for r in rows:
            nets[r["name"]] += Decimal(str(r["debit"])) - Decimal(str(r["credit"]))
        assert dict(nets) and all(v == Decimal("0") for v in nets.values())
        reversal = [r for r in rows
                    if r["name"] == "Garnishments Payable" and r["debit"] == "200.00"]
        assert len(reversal) == 1
        original = [r for r in rows
                    if r["name"] == "Garnishments Payable" and r["credit"] == "200.00"]
        assert len(original) == 1
        assert conn.execute(
            "SELECT cumulative_paid FROM wage_garnishment WHERE id = ?",
            (gid,)).fetchone()["cumulative_paid"] == "0.00"
