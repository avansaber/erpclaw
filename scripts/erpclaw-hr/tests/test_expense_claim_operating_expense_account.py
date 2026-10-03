"""Expense claims must debit an operating expense account.

A claim line and every account approval would debit must be an operating
expense account of the claim's company: never cost of goods sold, stock
adjustment, depreciation, exchange gain/loss, disposal gain/loss, rounding,
and never an asset, liability, equity or income account.
"""
import json

from erpclaw_lib.query import P, Q, Table

from hr_helpers import (
    call_action,
    is_error,
    is_ok,
    load_db_query,
    ns,
    seed_account,
    seed_company,
    seed_cost_center,
    seed_fiscal_year,
    seed_naming_series,
)

mod = load_db_query()

CLAIM_DATE = "2026-03-01"


def _ok(result):
    assert is_ok(result), result
    return result


def _employee(conn, env, first, last):
    return _ok(call_action(mod.add_employee, conn, ns(
        first_name=first, last_name=last, date_of_birth=None, gender=None,
        date_of_joining="2025-01-01", employment_type=None,
        company_id=env["company_id"], department_id=None, designation_id=None,
        employee_grade_id=None, branch=None, reporting_to=None, company_email=None,
        personal_email=None, cell_phone=None, emergency_contact=None,
        bank_details=None, ssn=None, federal_filing_status=None, w4_allowances=None,
        holiday_list_id=None, payroll_cost_center_id=None)))["employee_id"]


def _add(conn, company_id, employee_id, items):
    return call_action(mod.add_expense_claim, conn, ns(
        employee_id=employee_id, expense_date=CLAIM_DATE,
        company_id=company_id, items=json.dumps(items)))


def _submit(conn, claim_id):
    return call_action(mod.submit_expense_claim, conn, ns(
        expense_claim_id=claim_id))


def _approve(conn, claim_id, approver_id):
    return call_action(mod.approve_expense_claim, conn, ns(
        expense_claim_id=claim_id, approved_by=approver_id))


def _table_count(conn, table):
    t = Table(table)
    q = Q.from_(t).select(t.id)
    return len(conn.execute(q.get_sql(), ()).fetchall())


def _naming_current(conn, company_id):
    t = Table("naming_series")
    q = (Q.from_(t).select(t.current_value)
         .where(t.entity_type == P())
         .where(t.company_id == P()))
    return conn.execute(q.get_sql(), ("expense_claim", company_id)).fetchone()["current_value"]


def _audit_count(conn, action):
    t = Table("audit_log")
    q = Q.from_(t).select(t.id).where(t.action == P())
    return len(conn.execute(q.get_sql(), (action,)).fetchall())


def _claim_row(conn, claim_id):
    t = Table("expense_claim")
    q = (Q.from_(t)
         .select(t.status, t.approved_by, t.approval_date, t.naming_series, t.total_amount)
         .where(t.id == P()))
    return conn.execute(q.get_sql(), (claim_id,)).fetchone()


def _gl_for(conn, claim_id):
    t = Table("gl_entry")
    q = (Q.from_(t)
         .select(t.account_id, t.debit, t.credit, t.cost_center_id,
                 t.party_type, t.party_id, t.voucher_type)
         .where(t.voucher_id == P()))
    return [dict(r) for r in conn.execute(q.get_sql(), (claim_id,)).fetchall()]


def _set_default_expense(conn, company_id, account_id):
    t = Table("company")
    q = Q.update(t).set(t.default_expense_account_id, P()).where(t.id == P())
    conn.execute(q.get_sql(), (account_id, company_id))
    conn.commit()


def _disable_account(conn, account_id):
    t = Table("account")
    q = Q.update(t).set(t.disabled, P()).where(t.id == P())
    conn.execute(q.get_sql(), (1, account_id))
    conn.commit()


def _repoint_item_by_type(conn, claim_id, expense_type, account_id):
    t = Table("expense_claim_item")
    q = (Q.from_(t).select(t.id)
         .where(t.expense_claim_id == P())
         .where(t.expense_type == P()))
    item_id = conn.execute(q.get_sql(), (claim_id, expense_type)).fetchone()["id"]
    u = Q.update(t).set(t.account_id, P()).where(t.id == P())
    conn.execute(u.get_sql(), (account_id, item_id))
    conn.commit()


def _repoint_item(conn, claim_id, account_id):
    t = Table("expense_claim_item")
    q = Q.from_(t).select(t.id).where(t.expense_claim_id == P())
    item_id = conn.execute(q.get_sql(), (claim_id,)).fetchone()["id"]
    u = Q.update(t).set(t.account_id, P()).where(t.id == P())
    conn.execute(u.get_sql(), (account_id, item_id))
    conn.commit()


def _operating_msg(name, phrase):
    return (f"Item 0: account '{name}' {phrase}, not an operating expense account. "
            "An expense claim must debit an operating expense account.")


def test_cogs_line_refused_at_add(conn, env):
    dana = _employee(conn, env, "Dana", "Reyes")
    cogs = seed_account(conn, env["company_id"], "Cost of Goods Sold",
                        "expense", "cost_of_goods_sold", "5010")
    claims_before = _table_count(conn, "expense_claim")
    items_before = _table_count(conn, "expense_claim_item")
    naming_before = _naming_current(conn, env["company_id"])
    r = _add(conn, env["company_id"], dana, [
        {"expense_type": "travel", "description": "Trip",
         "amount": "150.00", "account_id": cogs}])
    assert is_error(r)
    assert r["message"] == _operating_msg("Cost of Goods Sold", "is a cost of goods sold account")
    assert _table_count(conn, "expense_claim") == claims_before
    assert _table_count(conn, "expense_claim_item") == items_before
    assert _naming_current(conn, env["company_id"]) == naming_before
    assert _audit_count(conn, "add-expense-claim") == 0


def test_other_kinds_refused_at_add(conn, env):
    dana = _employee(conn, env, "Dana", "Reyes")
    stock = seed_account(conn, env["company_id"], "Stock Adjustment",
                         "expense", "stock_adjustment", "5020")
    depr = seed_account(conn, env["company_id"], "Depreciation Expense",
                        "expense", "depreciation", "5030")
    exch = seed_account(conn, env["company_id"], "Exchange Loss",
                        "expense", "exchange_gain_loss", "5040")
    equip = seed_account(conn, env["company_id"], "Office Equipment",
                         "asset", "fixed_asset", "1500")
    old = seed_account(conn, env["company_id"], "Old Travel",
                       "expense", "expense", "5050")
    _disable_account(conn, old)
    cases = [
        (stock, "Stock Adjustment", "is a stock adjustment account"),
        (depr, "Depreciation Expense", "is a depreciation account"),
        (exch, "Exchange Loss", "is an exchange gain/loss account"),
        (equip, "Office Equipment", "is an asset account"),
        (old, "Old Travel", "is disabled"),
    ]
    for account_id, name, phrase in cases:
        claims_before = _table_count(conn, "expense_claim")
        items_before = _table_count(conn, "expense_claim_item")
        naming_before = _naming_current(conn, env["company_id"])
        r = _add(conn, env["company_id"], dana, [
            {"expense_type": "travel", "description": "Trip",
             "amount": "150.00", "account_id": account_id}])
        assert is_error(r), (name, r)
        assert r["message"] == _operating_msg(name, phrase), (name, r)
        assert _table_count(conn, "expense_claim") == claims_before
        assert _table_count(conn, "expense_claim_item") == items_before
        assert _naming_current(conn, env["company_id"]) == naming_before
        assert _audit_count(conn, "add-expense-claim") == 0


def test_cogs_default_refused_at_approve(conn, env):
    dana = _employee(conn, env, "Dana", "Reyes")
    evan = _employee(conn, env, "Evan", "Cole")
    cogs = seed_account(conn, env["company_id"], "Cost of Goods Sold",
                        "expense", "cost_of_goods_sold", "5010")
    _set_default_expense(conn, env["company_id"], cogs)
    claim_id = _ok(_add(conn, env["company_id"], dana, [
        {"expense_type": "travel", "description": "Trip", "amount": "150.00"}]))["expense_claim_id"]
    naming = _claim_row(conn, claim_id)["naming_series"]
    _ok(_submit(conn, claim_id))
    r = _approve(conn, claim_id, evan)
    assert is_error(r), r
    assert r["message"] == (
        f"Cannot approve expense claim {naming}: the company default expense account "
        "'Cost of Goods Sold' is a cost of goods sold account, "
        "not an operating expense account. Nothing was posted."), r
    row = _claim_row(conn, claim_id)
    assert row["status"] == "submitted"
    assert row["approved_by"] is None
    assert row["approval_date"] is None
    assert _gl_for(conn, claim_id) == []
    assert _audit_count(conn, "approve-expense-claim") == 0


def test_legacy_line_refused_at_approve(conn, env):
    dana = _employee(conn, env, "Dana", "Reyes")
    evan = _employee(conn, env, "Evan", "Cole")
    travel = seed_account(conn, env["company_id"], "Travel Expense",
                          "expense", "expense", "5100")
    cogs = seed_account(conn, env["company_id"], "Cost of Goods Sold",
                        "expense", "cost_of_goods_sold", "5010")
    claim_id = _ok(_add(conn, env["company_id"], dana, [
        {"expense_type": "travel", "description": "Trip",
         "amount": "150.00", "account_id": travel}]))["expense_claim_id"]
    _repoint_item(conn, claim_id, cogs)
    naming = _claim_row(conn, claim_id)["naming_series"]
    _ok(_submit(conn, claim_id))
    r = _approve(conn, claim_id, evan)
    assert is_error(r), r
    assert r["message"] == (
        f"Cannot approve expense claim {naming}: line account 'Cost of Goods Sold' "
        "is a cost of goods sold account, not an operating expense account. "
        "Nothing was posted."), r
    row = _claim_row(conn, claim_id)
    assert row["status"] == "submitted"
    assert row["approved_by"] is None
    assert row["approval_date"] is None
    assert _gl_for(conn, claim_id) == []
    assert _audit_count(conn, "approve-expense-claim") == 0


def test_several_problems_at_approve(conn, env):
    dana = _employee(conn, env, "Dana", "Reyes")
    evan = _employee(conn, env, "Evan", "Cole")
    travel = seed_account(conn, env["company_id"], "Travel Expense",
                          "expense", "expense", "5100")
    stock = seed_account(conn, env["company_id"], "Stock Adjustment",
                         "expense", "stock_adjustment", "5020")
    cogs = seed_account(conn, env["company_id"], "Cost of Goods Sold",
                        "expense", "cost_of_goods_sold", "5010")
    _set_default_expense(conn, env["company_id"], cogs)
    claim_id = _ok(_add(conn, env["company_id"], dana, [
        {"expense_type": "travel", "description": "Count",
         "amount": "100.00", "account_id": travel},
        {"expense_type": "meals", "description": "Dinner", "amount": "50.00"},
    ]))["expense_claim_id"]
    _repoint_item_by_type(conn, claim_id, "travel", stock)
    naming = _claim_row(conn, claim_id)["naming_series"]
    _ok(_submit(conn, claim_id))
    r = _approve(conn, claim_id, evan)
    assert is_error(r), r
    assert r["message"] == (
        f"Cannot approve expense claim {naming}: the company default expense account "
        "'Cost of Goods Sold' is a cost of goods sold account, "
        "not an operating expense account; line account 'Stock Adjustment' "
        "is a stock adjustment account, not an operating expense account. "
        "Nothing was posted."), r
    row = _claim_row(conn, claim_id)
    assert row["status"] == "submitted"
    assert row["approved_by"] is None
    assert row["approval_date"] is None
    assert _gl_for(conn, claim_id) == []
    assert _audit_count(conn, "approve-expense-claim") == 0


def test_correct_account_posts(conn, env):
    dana = _employee(conn, env, "Dana", "Reyes")
    evan = _employee(conn, env, "Evan", "Cole")
    travel = seed_account(conn, env["company_id"], "Travel Expense",
                          "expense", "expense", "5100")
    claim_id = _ok(_add(conn, env["company_id"], dana, [
        {"expense_type": "travel", "description": "Trip",
         "amount": "150.00", "account_id": travel}]))["expense_claim_id"]
    _ok(_submit(conn, claim_id))
    r = _approve(conn, claim_id, evan)
    assert is_ok(r), r
    legs = _gl_for(conn, claim_id)
    assert len(legs) == 2
    by_account = {leg["account_id"]: leg for leg in legs}
    assert by_account[travel]["debit"] == "150.00"
    assert by_account[travel]["credit"] == "0.00"
    assert by_account[travel]["cost_center_id"] == env["cost_center_id"]
    assert by_account[travel]["party_type"] is None
    assert by_account[travel]["party_id"] is None
    assert by_account[env["payable_account"]]["debit"] == "0.00"
    assert by_account[env["payable_account"]]["credit"] == "150.00"
    assert by_account[env["payable_account"]]["party_type"] == "employee"
    assert by_account[env["payable_account"]]["party_id"] == dana


def test_second_company_default_does_not_leak(conn, env):
    other = seed_company(conn, name="Other Co", abbr="OC")
    seed_fiscal_year(conn, other)
    seed_cost_center(conn, other, "Other CC")
    seed_naming_series(conn, other)
    other_cogs = seed_account(conn, other, "Cost of Goods Sold",
                              "expense", "cost_of_goods_sold", "5010")
    _set_default_expense(conn, other, other_cogs)
    dana = _employee(conn, env, "Dana", "Reyes")
    evan = _employee(conn, env, "Evan", "Cole")
    claim_id = _ok(_add(conn, env["company_id"], dana, [
        {"expense_type": "travel", "description": "Trip", "amount": "150.00"}]))["expense_claim_id"]
    _ok(_submit(conn, claim_id))
    r = _approve(conn, claim_id, evan)
    assert is_ok(r), r
    legs = _gl_for(conn, claim_id)
    by_account = {leg["account_id"]: leg for leg in legs}
    assert by_account[env["expense_account"]]["debit"] == "150.00"
    assert by_account[env["expense_account"]]["credit"] == "0.00"
    assert other_cogs not in by_account


def test_second_company_account_refused_at_add(conn, env):
    dana = _employee(conn, env, "Dana", "Reyes")
    other = seed_company(conn, name="Other Co", abbr="OC")
    seed_naming_series(conn, other)
    other_expense = seed_account(conn, other, "Other Travel",
                                 "expense", "expense", "5100")
    claims_before = _table_count(conn, "expense_claim")
    r = _add(conn, env["company_id"], dana, [
        {"expense_type": "travel", "description": "Trip",
         "amount": "150.00", "account_id": other_expense}])
    assert is_error(r), r
    assert r["message"] == f"Item 0: account {other_expense} belongs to a different company"
    assert _table_count(conn, "expense_claim") == claims_before
