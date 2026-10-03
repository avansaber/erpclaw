"""Strong-depth pins for seven payroll read actions.

Each test deepens the existing behavioural test for its action with a
fresh-seam read-back of the exact stored rows, a hand-written money
literal where money exists, an exact-message refusal (or a pinned empty
result where the action has no refusal branch), an unchanged-table set,
a read-computed derived value (computed by the read action's own code), and decoys that differ on the filtered
column (plus a second-company decoy wherever the action takes a company).

Existing tests are left in place; the assertion named in CHANGES.md
carries the weight for each action.
"""
import json
from decimal import Decimal

from payroll_helpers import (
    call_action, ns, is_error, is_ok, load_db_query,
    seed_company, seed_employee,
)
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, Table, P, fn, Field

mod = load_db_query()

PAYROLL_READ_TABLES = (
    "salary_component",
    "salary_structure",
    "salary_structure_detail",
    "salary_assignment",
    "wage_garnishment",
    "employee_bank_account",
    "audit_log",
)


def _fresh_count(table_name, column=None, value=None):
    conn2 = get_connection()
    try:
        t = Table(table_name)
        q = Q.from_(t).select(fn.Count("*").as_("cnt"))
        params = ()
        if column is not None:
            q = q.where(Field(column) == P())
            params = (value,)
        return conn2.execute(q.get_sql(), params).fetchone()["cnt"]
    finally:
        conn2.close()


def _fresh_row(table_name, row_id):
    conn2 = get_connection()
    try:
        t = Table(table_name)
        q = Q.from_(t).select(t.star).where(t.id == P())
        row = conn2.execute(q.get_sql(), (row_id,)).fetchone()
        return dict(row) if row is not None else None
    finally:
        conn2.close()


def _fresh_filtered(table_name, column, value):
    conn2 = get_connection()
    try:
        t = Table(table_name)
        q = Q.from_(t).select(t.star).where(Field(column) == P())
        return [dict(r) for r in conn2.execute(q.get_sql(), (value,)).fetchall()]
    finally:
        conn2.close()


def _all_rows(table_name):
    conn2 = get_connection()
    try:
        blk = Table(table_name)
        q = Q.from_(blk).select(blk.star).orderby(blk.id)
        return [dict(r) for r in conn2.execute(q.get_sql(), ()).fetchall()]
    finally:
        conn2.close()


def _snapshot(tables):
    return {name: _all_rows(name) for name in tables}


def _set_created_at(conn, table_name, row_id, created_at):
    blk = Table(table_name)
    q = Q.update(blk).set(Field("created_at"), P()).where(Field("id") == P())
    conn.execute(q.get_sql(), (created_at, row_id))
    conn.commit()


def _add_component(conn, name, component_type="earning"):
    result = call_action(mod.add_salary_component, conn, ns(
        name=name, component_type=component_type,
        is_tax_applicable=None, is_statutory=None, is_pre_tax=None,
        variable_based_on_taxable_salary=None, depends_on_payment_days=None,
        gl_account_id=None, description=None))
    assert is_ok(result), result
    return result["salary_component_id"]


def _add_structure(conn, name, company_id, details):
    result = call_action(mod.add_salary_structure, conn, ns(
        name=name, company_id=company_id,
        components=json.dumps(details), payroll_frequency=None))
    assert is_ok(result), result
    return result["salary_structure_id"]


def _add_assignment(conn, employee_id, structure_id, base, start, end=None):
    result = call_action(mod.add_salary_assignment, conn, ns(
        employee_id=employee_id, salary_structure_id=structure_id,
        base_amount=base, effective_from=start, effective_to=end))
    assert is_ok(result), result
    return result["salary_assignment_id"]


def _add_garnishment(conn, employee_id, order, creditor, gtype, amount,
                     total, start, pct=False, end=None):
    result = call_action(mod.add_garnishment, conn, ns(
        employee_id=employee_id, order_number=order, creditor_name=creditor,
        garnishment_type=gtype, amount_or_percentage=amount,
        is_percentage=pct, total_owed=total, start_date=start,
        end_date=end))
    assert is_ok(result), result
    return result["garnishment_id"]


class TestGetGarnishmentStrong:
    def test_exact_row_back_and_refusal(self, conn, env):
        decoy = _add_garnishment(
            conn, env["employee_id"], "CR-STRONG-002", "Credit Corp",
            "creditor", "15", "8000", "2026-04-01", pct=True)
        target = _add_garnishment(
            conn, env["employee_id"], "CS-STRONG-001", "State Support Unit",
            "child_support", "250.00", "1200.00", "2026-01-01")
        assert decoy != target

        before = _snapshot(PAYROLL_READ_TABLES)
        result = call_action(mod.get_garnishment, conn, ns(
            garnishment_id=target))
        assert is_ok(result), result
        assert result["id"] == target
        assert result["amount_or_percentage"] == "250.00"
        assert result["total_owed"] == "1200.00"
        assert result["priority"] == 1
        assert result["max_percentage"] == "50"
        assert result["creditor_name"] == "State Support Unit"
        assert result["garnishment_type"] == "child_support"
        assert result["status"] == "ok"
        assert result["document_status"] == "active"
        assert result["is_percentage"] == 0
        assert result["company_id"] == env["company_id"]
        assert result["cumulative_paid"] == "0"

        stored = _fresh_row("wage_garnishment", target)
        assert stored is not None
        for key in ("id", "employee_id", "order_number", "creditor_name",
                    "garnishment_type", "amount_or_percentage",
                    "is_percentage", "max_percentage", "priority",
                    "cumulative_paid", "total_owed", "start_date",
                    "company_id"):
            assert result[key] == stored[key]
        assert result["document_status"] == stored["status"]
        assert stored["status"] == "active"
        assert stored["amount_or_percentage"] == "250.00"
        assert _fresh_row("wage_garnishment", decoy)["amount_or_percentage"] == "15"
        assert _snapshot(PAYROLL_READ_TABLES) == before

        refused = call_action(mod.get_garnishment, conn, ns(
            garnishment_id="no-such-garnishment"))
        assert is_error(refused)
        assert refused["message"] == "Garnishment no-such-garnishment not found"
        assert _snapshot(PAYROLL_READ_TABLES) == before


class TestGetSalaryStructureStrong:
    def test_breakdown_back_and_refusal(self, conn, env):
        base = _add_component(conn, "Strong Base")
        allow = _add_component(conn, "Strong Allow")
        other_company = seed_company(conn)
        decoy_struct = _add_structure(conn, "Decoy Structure", other_company, [
            {"salary_component_id": base, "amount": "999"},
        ])
        struct = _add_structure(conn, "Strong Structure", env["company_id"], [
            {"salary_component_id": base, "amount": "5000"},
            {"salary_component_id": allow, "percentage": "40",
             "base_component_id": base},
        ])
        assert decoy_struct != struct

        before = _snapshot(PAYROLL_READ_TABLES)
        result = call_action(mod.get_salary_structure, conn, ns(
            salary_structure_id=struct))
        assert is_ok(result), result
        header = result["salary_structure"]
        assert header["id"] == struct
        assert header["name"] == "Strong Structure"
        assert header["company_id"] == env["company_id"]
        assert header["payroll_frequency"] == "monthly"
        assert header["component_count"] == 2
        assert [c["component_name"] for c in header["components"]] == [
            "Strong Base", "Strong Allow"]
        assert header["components"][0]["amount"] == "5000"
        assert header["components"][1]["percentage"] == "40"
        assert header["components"][1]["base_component_name"] == "Strong Base"

        assert _fresh_row("salary_structure", struct)["name"] == "Strong Structure"
        details = _fresh_filtered(
            "salary_structure_detail", "salary_structure_id", struct)
        assert len(details) == 2
        by_comp = {d["salary_component_id"]: d for d in details}
        assert by_comp[base]["amount"] == "5000"
        assert by_comp[allow]["percentage"] == "40"
        assert by_comp[allow]["base_component_id"] == base
        assert _snapshot(PAYROLL_READ_TABLES) == before

        refused = call_action(mod.get_salary_structure, conn, ns(
            salary_structure_id="no-such-structure"))
        assert is_error(refused)
        assert refused["message"] == "Salary structure no-such-structure not found"
        assert _snapshot(PAYROLL_READ_TABLES) == before


class TestListEmployeeBankAccountsStrong:
    def test_masked_values_back_and_refusal(self, conn):
        company_a = seed_company(conn)
        emp_a = seed_employee(conn, company_a)
        emp_b = seed_employee(conn, company_a, first_name="Jane",
                              last_name="Smith")
        company_b = seed_company(conn)
        emp_c = seed_employee(conn, company_b)

        def _add_account(employee_id, bank, routing, number):
            out = call_action(mod.add_employee_bank_account, conn, ns(
                employee_id=employee_id, bank_name=bank,
                routing_number=routing, account_number=number,
                account_type="checking"))
            assert is_ok(out), out
            return out["employee_bank_account_id"]

        target = _add_account(emp_a, "First Strong Bank", "021000021",
                              "123456789")
        _add_account(emp_b, "Second Bank", "011401025", "987654321")
        _add_account(emp_c, "Far Bank", "021000021", "555566667")

        before = _snapshot(PAYROLL_READ_TABLES)
        result = call_action(mod.list_employee_bank_accounts, conn, ns(
            employee_id=emp_a))
        assert is_ok(result), result
        assert result["count"] == 1
        assert len(result["accounts"]) == 1
        account = result["accounts"][0]
        assert account["id"] == target
        assert account["bank_name"] == "First Strong Bank"
        assert account["account_number_masked"] == "****6789"
        assert account["account_number"] == "****6789"
        assert account["routing_number"] == "021000021"
        assert account["account_type"] == "checking"
        assert account["employee_id"] == emp_a

        stored = _fresh_filtered(
            "employee_bank_account", "employee_id", emp_a)
        assert len(stored) == 1
        assert stored[0]["id"] == target
        assert stored[0]["bank_name"] == "First Strong Bank"
        assert _fresh_count("employee_bank_account") == 3
        assert _snapshot(PAYROLL_READ_TABLES) == before

        refused = call_action(mod.list_employee_bank_accounts, conn, ns(
            employee_id=None))
        assert is_error(refused)
        assert refused["message"] == "--employee-id is required"
        assert _snapshot(PAYROLL_READ_TABLES) == before

        empty = call_action(mod.list_employee_bank_accounts, conn, ns(
            employee_id="no-such-employee"))
        assert is_ok(empty)
        assert empty["count"] == 0
        assert empty["accounts"] == []
        assert _snapshot(PAYROLL_READ_TABLES) == before


class TestListGarnishmentsStrong:
    def test_filtered_counts_ordering_and_empty_pin(self, conn, env):
        emp_b = seed_employee(conn, env["company_id"], first_name="Jane",
                              last_name="Smith")
        company_b = seed_company(conn)
        emp_c = seed_employee(conn, company_b)
        _add_garnishment(conn, env["employee_id"], "TL-S4-002", "IRS",
                         "tax_levy", "100.00", "5000", "2026-02-01")
        _add_garnishment(conn, env["employee_id"], "CS-S4-001",
                         "State Support Unit", "child_support", "250.00",
                         "1200.00", "2026-01-01")
        _add_garnishment(conn, emp_b, "CR-S4-003", "Credit Corp", "creditor",
                         "15", "8000", "2026-03-01", pct=True)
        _add_garnishment(conn, emp_c, "CS-S4-004", "Other State",
                         "child_support", "999.00", "9000", "2026-01-01")

        before = _snapshot(PAYROLL_READ_TABLES)
        result = call_action(mod.list_garnishments, conn, ns(
            employee_id=env["employee_id"], company_id=None, status=None))
        assert is_ok(result), result
        assert result["count"] == 2
        assert [g["garnishment_type"] for g in result["garnishments"]] == [
            "child_support", "tax_levy"]
        assert [g["amount_or_percentage"] for g in result["garnishments"]] == [
            "250.00", "100.00"]
        # Sum computed by the test, not by the product.
        total = sum((Decimal(g["amount_or_percentage"])
                     for g in result["garnishments"]), Decimal("0"))
        assert str(total) == "350.00"

        stored = _fresh_filtered(
            "wage_garnishment", "employee_id", env["employee_id"])
        assert len(stored) == 2
        assert sorted(s["amount_or_percentage"] for s in stored) == [
            "100.00", "250.00"]
        assert _fresh_count("wage_garnishment") == 4

        by_company = call_action(mod.list_garnishments, conn, ns(
            employee_id=None, company_id=env["company_id"], status=None))
        assert is_ok(by_company), by_company
        assert by_company["count"] == 3
        by_status = call_action(mod.list_garnishments, conn, ns(
            employee_id=env["employee_id"], company_id=None,
            status="paused"))
        assert is_ok(by_status), by_status
        assert by_status["count"] == 0
        assert by_status["garnishments"] == []
        assert _snapshot(PAYROLL_READ_TABLES) == before

        empty = call_action(mod.list_garnishments, conn, ns(
            employee_id="no-such-employee", company_id=None, status=None))
        assert is_ok(empty)
        assert empty["count"] == 0
        assert empty["garnishments"] == []
        assert _snapshot(PAYROLL_READ_TABLES) == before

    def test_invalid_status_refusal(self, conn, env):
        _add_garnishment(conn, env["employee_id"], "CS-GUARD-001",
                         "State Support Unit", "child_support", "250.00",
                         "1200.00", "2026-01-01")
        before = _snapshot(PAYROLL_READ_TABLES)
        refused = call_action(mod.list_garnishments, conn, ns(
            employee_id=None, company_id=None, status="bogus"))
        assert is_error(refused)
        assert refused["message"] == (
            "Invalid status: bogus. Must be one of: active, paused, "
            "completed, cancelled")
        assert _snapshot(PAYROLL_READ_TABLES) == before

    def test_empty_string_status_means_no_filter(self, conn, env):
        _add_garnishment(conn, env["employee_id"], "TL-S4-002", "IRS",
                         "tax_levy", "100.00", "5000", "2026-02-01")
        _add_garnishment(conn, env["employee_id"], "CS-S4-001",
                         "State Support Unit", "child_support", "250.00",
                         "1200.00", "2026-01-01")

        before = _snapshot(PAYROLL_READ_TABLES)
        unfiltered = call_action(mod.list_garnishments, conn, ns(
            employee_id=env["employee_id"], company_id=None, status=None))
        assert is_ok(unfiltered), unfiltered
        assert unfiltered["count"] == 2

        by_empty_status = call_action(mod.list_garnishments, conn, ns(
            employee_id=env["employee_id"], company_id=None, status=""))
        assert is_ok(by_empty_status), by_empty_status
        assert by_empty_status["garnishments"] == unfiltered["garnishments"]
        assert by_empty_status["count"] == unfiltered["count"]
        assert [g["id"] for g in by_empty_status["garnishments"]] == [
            g["id"] for g in unfiltered["garnishments"]]
        assert _snapshot(PAYROLL_READ_TABLES) == before


class TestListSalaryAssignmentsStrong:
    def test_company_scoped_ordering_and_empty_pin(self, conn, env):
        comp = _add_component(conn, "Assign Strong Base")
        struct = _add_structure(conn, "Assign Strong Structure",
                                env["company_id"], [
                                    {"salary_component_id": comp,
                                     "amount": "4000"},
                                ])
        _add_assignment(conn, env["employee_id"], struct, "4000",
                        "2026-01-01")
        _add_assignment(conn, env["employee_id"], struct, "4500.50",
                        "2026-06-01")
        emp_b = seed_employee(conn, env["company_id"], first_name="Jane",
                              last_name="Smith")
        _add_assignment(conn, emp_b, struct, "3000", "2026-01-01")
        company_b = seed_company(conn)
        emp_c = seed_employee(conn, company_b)
        struct_b = _add_structure(conn, "Other Company Structure", company_b,
                                  [{"salary_component_id": comp,
                                    "amount": "1000"}])
        _add_assignment(conn, emp_c, struct_b, "1000", "2026-01-01")

        before = _snapshot(PAYROLL_READ_TABLES)
        result = call_action(mod.list_salary_assignments, conn, ns(
            employee_id=None, company_id=env["company_id"], limit=20,
            offset=0, from_date=None, to_date=None))
        assert is_ok(result), result
        assert result["count"] == 3
        assert result["has_more"] is False
        mine = [a for a in result["assignments"]
                if a["employee_id"] == env["employee_id"]]
        assert [a["base_amount"] for a in mine] == ["4500.50", "4000.00"]
        assert [a["effective_from"] for a in mine] == [
            "2026-06-01", "2026-01-01"]
        assert mine[0]["employee_name"] == "John Doe"
        assert mine[0]["salary_structure_name"] == "Assign Strong Structure"

        stored = _fresh_filtered(
            "salary_assignment", "company_id", env["company_id"])
        assert len(stored) == 3
        mine_stored = sorted(
            (s for s in stored if s["employee_id"] == env["employee_id"]),
            key=lambda s: s["effective_from"], reverse=True)
        assert [s["base_amount"] for s in mine_stored] == [
            "4500.50", "4000.00"]
        assert result["assignments"][0]["base_amount"] == mine_stored[0][
            "base_amount"]
        assert _snapshot(PAYROLL_READ_TABLES) == before

        by_employee = call_action(mod.list_salary_assignments, conn, ns(
            employee_id=env["employee_id"], company_id=None, limit=20,
            offset=0, from_date=None, to_date=None))
        assert is_ok(by_employee), by_employee
        assert by_employee["count"] == 2

        empty = call_action(mod.list_salary_assignments, conn, ns(
            employee_id=None, company_id="no-such-company", limit=20,
            offset=0, from_date=None, to_date=None))
        assert is_ok(empty)
        assert empty["count"] == 0
        assert empty["assignments"] == []
        assert _snapshot(PAYROLL_READ_TABLES) == before


class TestListSalaryComponentsStrong:
    def test_type_filter_ordering_and_refusal(self, conn):
        _add_component(conn, "Zebra Earn")
        _add_component(conn, "Alpha Earn")
        _add_component(conn, "Beta Deduct", component_type="deduction")

        before = _snapshot(PAYROLL_READ_TABLES)
        result = call_action(mod.list_salary_components, conn, ns(
            component_type="earning", limit=20, offset=0, search=None))
        assert is_ok(result), result
        assert result["count"] == 2
        assert [c["name"] for c in result["components"]] == [
            "Alpha Earn", "Zebra Earn"]
        assert result["has_more"] is False

        page = call_action(mod.list_salary_components, conn, ns(
            component_type="earning", limit=1, offset=0, search=None))
        assert is_ok(page), page
        assert page["count"] == 2
        assert len(page["components"]) == 1
        assert page["has_more"] is True
        assert page["components"][0]["name"] == "Alpha Earn"

        stored = _fresh_filtered(
            "salary_component", "component_type", "earning")
        assert sorted(s["name"] for s in stored) == [
            "Alpha Earn", "Zebra Earn"]
        assert _fresh_count("salary_component") == 3
        assert _snapshot(PAYROLL_READ_TABLES) == before

        refused = call_action(mod.list_salary_components, conn, ns(
            component_type="bogus", limit=20, offset=0, search=None))
        assert is_error(refused)
        assert refused["message"] == (
            "Invalid component type filter 'bogus'. "
            "Valid: ('earning', 'deduction', 'employer_contribution')")
        assert _snapshot(PAYROLL_READ_TABLES) == before


class TestListSalaryStructuresStrong:
    def test_company_scoped_counts_and_empty_pin(self, conn, env):
        comp_a = _add_component(conn, "S7 Base")
        comp_b = _add_component(conn, "S7 Bonus")
        _add_structure(conn, "Beta Structure", env["company_id"], [
            {"salary_component_id": comp_a, "amount": "750"},
        ])
        alpha = _add_structure(conn, "Alpha Structure", env["company_id"], [
            {"salary_component_id": comp_a, "amount": "5000"},
            {"salary_component_id": comp_b, "amount": "1000"},
        ])
        company_b = seed_company(conn)
        _add_structure(conn, "Gamma Structure", company_b, [
            {"salary_component_id": comp_a, "amount": "999"},
        ])

        before = _snapshot(PAYROLL_READ_TABLES)
        result = call_action(mod.list_salary_structures, conn, ns(
            company_id=env["company_id"], limit=20, offset=0, search=None))
        assert is_ok(result), result
        assert result["count"] == 2
        assert [s["name"] for s in result["structures"]] == [
            "Alpha Structure", "Beta Structure"]
        assert [s["component_count"] for s in result["structures"]] == [2, 1]
        assert sum(s["component_count"] for s in result["structures"]) == 3
        assert result["has_more"] is False

        stored = _fresh_filtered(
            "salary_structure", "company_id", env["company_id"])
        assert len(stored) == 2
        details = _fresh_filtered(
            "salary_structure_detail", "salary_structure_id", alpha)
        assert sorted(d["amount"] for d in details) == ["1000", "5000"]
        assert _fresh_count("salary_structure") == 3
        assert _snapshot(PAYROLL_READ_TABLES) == before

        empty = call_action(mod.list_salary_structures, conn, ns(
            company_id="no-such-company", limit=20, offset=0, search=None))
        assert is_ok(empty)
        assert empty["count"] == 0
        assert empty["structures"] == []
        assert _snapshot(PAYROLL_READ_TABLES) == before
