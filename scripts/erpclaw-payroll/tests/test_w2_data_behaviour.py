"""Part A: behaviour of generate-w2-data, read back against a fully known payroll.

Every salary slip below is produced by the module's own actions
(create-payroll-run, generate-salary-slips, submit-payroll-run,
cancel-payroll-run), with fixed dates. Federal withholding comes from one flat
10 % federal slab with no standard deduction, so each figure is a closed-form
product of the base pay.

Company A ("env"), monthly runs, base pay from the salary assignment:

  John Doe     5000.00, no 401k
  Alice Baker  7000.00, 401k at 5 % of gross (350.00 a month)

  2025-12  submitted    (the 2025 tax year)
  2026-01  submitted
  2026-02  submitted
  2026-03  submitted, then CANCELLED (slips marked cancelled, ledger reversed)
  2026-04  generated, never submitted (slips stay draft)

Company B: its own employee (also named John Doe) at 4000.00, 2026-01 submitted.

Company C: its own employee (John Doe again) at 2000.00 a period, one biweekly
run from 2025-12-22 to 2026-01-04, submitted. A slip belongs to the tax year its
period ends in, which is the year generate-salary-slips already takes its FICA
wage base and year-to-date figures from, so this slip is on the 2026 W-2 and not
on the 2025 one.

FICA for 2026 uses a 12000 Social Security wage base so Alice's February slip
is capped (5000.00 taxable, 310.00 tax) and her box 3 is 12000.00; 2025 uses
168600. The W-2 counts only submitted slips of the company whose period ends
in the tax year: 2026 for company A is January plus February.
"""
import json

from payroll_helpers import (
    build_payroll_env, call_action, is_error, is_ok, load_db_query, ns,
    seed_employee, seed_fiscal_year,
)

mod = load_db_query()


# ── helpers ────────────────────────────────────────────────────────────────

def _ok(result):
    assert is_ok(result), result
    return result


def _fica(conn, tax_year, wage_base):
    _ok(call_action(mod.update_fica_config, conn, ns(
        tax_year=tax_year, ss_wage_base=wage_base, ss_employee_rate="6.2",
        ss_employer_rate="6.2", medicare_employee_rate="1.45",
        medicare_employer_rate="1.45", additional_medicare_threshold="200000",
        additional_medicare_rate="0.9")))


def _structure(conn, company_id, component_id, name):
    return _ok(call_action(mod.add_salary_structure, conn, ns(
        name=name, company_id=company_id, payroll_frequency=None,
        components=json.dumps([{"salary_component_id": component_id, "amount": "0"}]),
    )))["salary_structure_id"]


def _assign(conn, employee_id, structure_id, base):
    _ok(call_action(mod.add_salary_assignment, conn, ns(
        employee_id=employee_id, salary_structure_id=structure_id,
        base_amount=base, effective_from="2025-12-01", effective_to=None)))


def _run(conn, env, start, end, submit=True, frequency="monthly"):
    run_id = _ok(call_action(mod.create_payroll_run, conn, ns(
        company_id=env["company_id"], period_start=start, period_end=end,
        department_id=None, payroll_frequency=frequency)))["payroll_run_id"]
    _ok(call_action(mod.generate_salary_slips, conn, ns(payroll_run_id=run_id)))
    if submit:
        _ok(call_action(mod.submit_payroll_run, conn, ns(
            payroll_run_id=run_id, cost_center_id=env["cost_center_id"])))
    return run_id


def _book(conn):
    a = build_payroll_env(conn)
    seed_fiscal_year(conn, a["company_id"], start="2025-01-01", end="2025-12-31")
    alice = seed_employee(conn, a["company_id"], first_name="Alice",
                          last_name="Baker", employee_401k_rate="5")
    b = build_payroll_env(conn)
    c = build_payroll_env(conn)

    _fica(conn, "2025", "168600")
    _fica(conn, "2026", "12000")
    _ok(call_action(mod.add_income_tax_slab, conn, ns(
        name="Flat federal", tax_jurisdiction="federal", effective_from="2025-01-01",
        filing_status=None, state_code=None, standard_deduction=None,
        rates=json.dumps([{"from_amount": "0", "to_amount": None, "rate": "10"}]))))
    comp = _ok(call_action(mod.add_salary_component, conn, ns(
        name="W2 Base Pay", component_type="earning", is_tax_applicable=None,
        is_statutory=None, is_pre_tax=None, variable_based_on_taxable_salary=None,
        depends_on_payment_days=None, gl_account_id=None,
        description=None)))["salary_component_id"]

    sa = _structure(conn, a["company_id"], comp, "W2 Structure A")
    _assign(conn, a["employee_id"], sa, "5000.00")
    _assign(conn, alice, sa, "7000.00")
    sb = _structure(conn, b["company_id"], comp, "W2 Structure B")
    _assign(conn, b["employee_id"], sb, "4000.00")
    sc = _structure(conn, c["company_id"], comp, "W2 Structure C")
    _assign(conn, c["employee_id"], sc, "2000.00")

    runs = {
        "dec": _run(conn, a, "2025-12-01", "2025-12-31"),
        "jan": _run(conn, a, "2026-01-01", "2026-01-31"),
        "feb": _run(conn, a, "2026-02-01", "2026-02-28"),
        "mar": _run(conn, a, "2026-03-01", "2026-03-31"),
    }
    _ok(call_action(mod.cancel_payroll_run, conn, ns(payroll_run_id=runs["mar"])))
    runs["apr"] = _run(conn, a, "2026-04-01", "2026-04-30", submit=False)
    runs["b_jan"] = _run(conn, b, "2026-01-01", "2026-01-31")
    runs["c_span"] = _run(conn, c, "2025-12-22", "2026-01-04", frequency="biweekly")
    return {"a": a, "b": b, "c": c, "john": a["employee_id"], "alice": alice,
            "b_john": b["employee_id"], "c_john": c["employee_id"], "runs": runs}


def _w2(conn, tax_year, company_id):
    return call_action(mod.generate_w2_data, conn, ns(tax_year=tax_year,
                                                      company_id=company_id))


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _slip_statuses(conn, run_id):
    rows = conn.execute(
        "SELECT employee_id, status, gross_pay FROM salary_slip WHERE payroll_run_id = ?",
        (run_id,)).fetchall()
    return sorted((r["employee_id"], r["status"], r["gross_pay"]) for r in rows)


# ── generate-w2-data ───────────────────────────────────────────────────────

def test_w2_boxes_per_employee_from_submitted_slips_only(conn):
    book = _book(conn)
    john, alice = book["john"], book["alice"]

    # The slips the report must choose between, read back from the table.
    assert _slip_statuses(conn, book["runs"]["mar"]) == sorted(
        [(john, "cancelled", "5000.00"), (alice, "cancelled", "7000.00")])
    assert _slip_statuses(conn, book["runs"]["apr"]) == sorted(
        [(john, "draft", "5000.00"), (alice, "draft", "7000.00")])
    assert _slip_statuses(conn, book["runs"]["feb"]) == sorted(
        [(john, "submitted", "5000.00"), (alice, "submitted", "7000.00")])

    r = _w2(conn, "2026", book["a"]["company_id"])
    assert is_ok(r)
    assert (r["tax_year"], r["company_id"], r["employee_count"]) == \
        (2026, book["a"]["company_id"], 2)
    assert r["w2_data"] == [
        {
            "employee_id": alice,
            "employee_name": "Alice Baker",
            "ssn_last_four": "XXXX",
            "filing_status": "single",
            "boxes": {"1": "13300.00", "2": "1330.00", "3": "12000.00",
                      "4": "744.00", "5": "14000.00", "6": "203.00",
                      "12": {"D": "700.00"}},
        },
        {
            "employee_id": john,
            "employee_name": "John Doe",
            "ssn_last_four": "XXXX",
            "filing_status": "single",
            "boxes": {"1": "10000.00", "2": "1000.00", "3": "10000.00",
                      "4": "620.00", "5": "10000.00", "6": "145.00"},
        },
    ]


def test_w2_box_figures_equal_the_submitted_slip_rows(conn):
    book = _book(conn)
    r = _w2(conn, "2026", book["a"]["company_id"])
    boxes = {e["employee_id"]: e["boxes"] for e in r["w2_data"]}

    def detail_total(employee_id, component):
        rows = conn.execute(
            """SELECT d.amount FROM salary_slip_detail d
               JOIN salary_slip s ON s.id = d.salary_slip_id
               JOIN salary_component c ON c.id = d.salary_component_id
               WHERE s.employee_id = ? AND s.status = 'submitted'
                 AND s.period_start >= '2026-01-01' AND c.name = ?""",
            (employee_id, component)).fetchall()
        return sorted(r["amount"] for r in rows)

    alice = book["alice"]
    assert detail_total(alice, "Federal Income Tax") == ["665.00", "665.00"]
    assert detail_total(alice, "Social Security Tax") == ["310.00", "434.00"]
    assert detail_total(alice, "Medicare Tax") == ["101.50", "101.50"]
    assert detail_total(alice, "401k Contribution") == ["350.00", "350.00"]
    gross = conn.execute(
        """SELECT gross_pay FROM salary_slip WHERE employee_id = ?
           AND status = 'submitted' AND period_start >= '2026-01-01'""",
        (alice,)).fetchall()
    assert sorted(g["gross_pay"] for g in gross) == ["7000.00", "7000.00"]
    assert boxes[alice]["5"] == "14000.00"
    assert boxes[alice]["4"] == "744.00"

    john = book["john"]
    assert detail_total(john, "Social Security Tax") == ["310.00", "310.00"]
    assert detail_total(john, "401k Contribution") == []
    assert "12" not in boxes[john]


def test_w2_scoped_to_tax_year_and_company(conn):
    book = _book(conn)

    r2025 = _w2(conn, "2025", book["a"]["company_id"])
    assert r2025["employee_count"] == 2
    assert [(e["employee_id"], e["boxes"]) for e in r2025["w2_data"]] == [
        (book["alice"], {"1": "6650.00", "2": "665.00", "3": "7000.00", "4": "434.00",
                         "5": "7000.00", "6": "101.50", "12": {"D": "350.00"}}),
        (book["john"], {"1": "5000.00", "2": "500.00", "3": "5000.00", "4": "310.00",
                        "5": "5000.00", "6": "72.50"}),
    ]

    rb = _w2(conn, "2026", book["b"]["company_id"])
    assert rb["employee_count"] == 1
    assert [(e["employee_id"], e["employee_name"], e["boxes"]) for e in rb["w2_data"]] == [
        (book["b_john"], "John Doe",
         {"1": "4000.00", "2": "400.00", "3": "4000.00", "4": "248.00",
          "5": "4000.00", "6": "58.00"}),
    ]

    r2024 = _w2(conn, "2024", book["a"]["company_id"])
    assert is_ok(r2024)
    assert (r2024["employee_count"], r2024["w2_data"]) == (0, [])


def test_w2_reports_a_year_spanning_slip_once_in_the_year_its_period_ends(conn):
    book = _book(conn)
    c_john = book["c_john"]
    assert _slip_statuses(conn, book["runs"]["c_span"]) == [(c_john, "submitted", "2000.00")]
    slip = conn.execute(
        "SELECT period_start, period_end FROM salary_slip WHERE payroll_run_id = ?",
        (book["runs"]["c_span"],)).fetchone()
    assert (slip["period_start"], slip["period_end"]) == ("2025-12-22", "2026-01-04")

    rc2026 = _w2(conn, "2026", book["c"]["company_id"])
    assert [(e["employee_id"], e["boxes"]) for e in rc2026["w2_data"]] == [
        (c_john, {"1": "2000.00", "2": "200.00", "3": "2000.00", "4": "124.00",
                  "5": "2000.00", "6": "29.00"}),
    ]
    rc2025 = _w2(conn, "2025", book["c"]["company_id"])
    assert is_ok(rc2025)
    assert (rc2025["employee_count"], rc2025["w2_data"]) == (0, [])


def test_w2_refusals_and_reads_write_nothing(conn):
    book = _book(conn)
    tables = ("salary_slip", "salary_slip_detail", "payroll_run", "gl_entry", "audit_log")
    counts = tuple(_count(conn, t) for t in tables)

    r = _w2(conn, None, book["a"]["company_id"])
    assert is_error(r) and r["message"] == "--tax-year is required"
    r = _w2(conn, "2026", None)
    assert is_error(r) and r["message"] == "--company-id is required"
    r = _w2(conn, "twenty", book["a"]["company_id"])
    assert is_error(r) and r["message"] == "--tax-year must be an integer, got: twenty"
    r = _w2(conn, "2026", "no-such-company")
    assert is_error(r) and r["message"] == "Company no-such-company not found"
    assert "w2_data" not in r

    assert is_ok(_w2(conn, "2026", book["a"]["company_id"]))
    assert tuple(_count(conn, t) for t in tables) == counts
