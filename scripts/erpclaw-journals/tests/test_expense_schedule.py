"""Exact monthly recognition drafts over the existing recurring-journal surface."""
import json
import os
from pathlib import Path
import subprocess
import sys
from decimal import Decimal

import pytest

from journals_helpers import build_journals_env, call_action, load_db_query, ns, seed_account
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, P, Table, Order

mod = load_db_query()


@pytest.fixture
def book(db_path):
    connection = get_connection(db_path)
    data = build_journals_env(connection)
    data["prepaid"] = seed_account(connection, data["company_id"], "Prepaid rent",
                                   "asset", None, "1300")
    data["accrued"] = seed_account(connection, data["company_id"], "Accrued rent",
                                   "liability", None, "2300")
    yield connection, data
    connection.close()


def args(data, **changes):
    values = dict(company_id=data["company_id"], template_name="Rent recognition",
                  schedule_kind="prepaid", amount="100.00", periods="3",
                  start_date="2026-01-31", expense_account_id=data["expense"],
                  balance_account_id=data["prepaid"], auto_submit=None, remark=None,
                  dimensions=json.dumps({"cost_center": data["cc"]}))
    values.update(changes)
    return ns(**values)


def rows(connection, table):
    t = Table(table)
    query = Q.from_(t).select(t.star).orderby(t.id, order=Order.asc)
    return [dict(row) for row in connection.execute(query.get_sql(), ()).fetchall()]


def snapshot(connection):
    return {name: rows(connection, name) for name in (
        "recurring_journal_template", "journal_entry", "journal_entry_line",
        "gl_entry", "audit_log", "naming_series")}


@pytest.mark.parametrize("kind,balance", [("prepaid", "prepaid"), ("accrual", "accrued")])
def test_exact_schedule_and_existing_due_processor(book, kind, balance):
    connection, data = book
    result = call_action(mod.add_expense_schedule, connection,
                         args(data, schedule_kind=kind, balance_account_id=data[balance]))
    assert result["status"] == "ok", result
    assert result["total"] == "100.00"
    assert [item["amount"] for item in result["templates"]] == ["33.34", "33.33", "33.33"]
    assert [item["due_date"] for item in result["templates"]] == [
        "2026-01-31", "2026-02-28", "2026-03-31"]
    stored = rows(connection, "recurring_journal_template")
    assert len(stored) == 3
    for item in stored:
        assert item["auto_submit"] == 0
        assert item["end_date"] == item["start_date"]
        lines = json.loads(item["lines"])
        assert lines[0]["account_id"] == data["expense"]
        assert lines[1]["account_id"] == data[balance]
        assert Decimal(lines[0]["debit"]) == Decimal(lines[1]["credit"])
        assert json.loads(item["dimensions_json"]) == {"cost_center": data["cc"]}
    assert rows(connection, "gl_entry") == []
    due = call_action(mod.process_recurring, connection,
                      ns(company_id=data["company_id"], as_of_date="2026-02-28", resume_run_id=None))
    assert due["status"] == "ok" and due["generated"] == 2 and due["errors"] == [], due
    last = call_action(mod.process_recurring, connection,
                       ns(company_id=data["company_id"], as_of_date="2026-03-31", resume_run_id=None))
    assert last["generated"] == 1 and last["errors"] == [], last
    again = call_action(mod.process_recurring, connection,
                        ns(company_id=data["company_id"], as_of_date="2026-03-31", resume_run_id=None))
    assert again["generated"] == 0 and again["run_status"] == "no_work"
    entries = rows(connection, "journal_entry")
    assert [entry["status"] for entry in entries] == ["draft"] * 3
    assert sum((Decimal(entry["total_debit"]) for entry in entries), Decimal("0")) == Decimal("100.00")
    assert rows(connection, "gl_entry") == []


@pytest.mark.parametrize("changes", [
    {"amount": "NaN"}, {"amount": "1.001"}, {"amount": "1e2"}, {"amount": "0"},
    {"amount": "0.02"}, {"amount": 12.0}, {"periods": "121"}, {"periods": "0"},
    {"periods": "2.0"}, {"schedule_kind": "other"}, {"start_date": "2026-02-30"},
    {"start_date": "20260131"}, {"start_date": "9999-12-31"}, {"auto_submit": True},
    {"expense_account_id": "missing"}, {"company_id": "missing"},
])
def test_invalid_schedule_refuses_without_mutation(book, changes):
    connection, data = book
    before = snapshot(connection)
    result = call_action(mod.add_expense_schedule, connection, args(data, **changes))
    assert result["status"] == "error", result
    assert snapshot(connection) == before


def test_company_and_account_root_refusals(book):
    connection, data = book
    foreign = build_journals_env(connection)
    for changes in ({"expense_account_id": foreign["expense"]},
                    {"balance_account_id": data["expense"]},
                    {"schedule_kind": "accrual", "balance_account_id": data["prepaid"]}):
        before = snapshot(connection)
        result = call_action(mod.add_expense_schedule, connection, args(data, **changes))
        assert result["status"] == "error", result
        assert snapshot(connection) == before


@pytest.mark.parametrize("center_kind", ["foreign", "group", "missing"])
def test_cost_center_scope_refuses_without_any_write(book, center_kind):
    connection, data = book
    if center_kind == "foreign":
        center_id = build_journals_env(connection)["cc"]
    elif center_kind == "group":
        center_id = data["cc"]
        center = Table("cost_center")
        connection.execute(Q.update(center).set(center.is_group, 1)
                           .where(center.id == P()).get_sql(), (center_id,))
        connection.commit()
    else:
        center_id = "missing"
    tables = ("company", "fiscal_year", "account", "cost_center",
              "recurring_journal_template", "journal_entry", "journal_entry_line",
              "gl_entry", "audit_log", "naming_series")
    before = {table: rows(connection, table) for table in tables}
    result = call_action(mod.add_expense_schedule, connection,
                         args(data, dimensions=json.dumps({"cost_center": center_id})))
    assert result["status"] == "error", result
    assert "leaf centre of this company" in result["message"]
    assert {table: rows(connection, table) for table in tables} == before


def test_audit_failure_rolls_back_every_period_and_name(book, monkeypatch):
    connection, data = book
    before = snapshot(connection)
    original = mod.audit
    calls = []

    def fail_second(*values, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise ValueError("planted second-period audit refusal")
        return original(*values, **kwargs)

    monkeypatch.setattr(mod, "audit", fail_second)
    with pytest.raises(ValueError, match="planted second-period"):
        mod.add_expense_schedule(connection, args(data))
    assert snapshot(connection) == before


def test_public_router_creates_exact_drafts_on_fresh_connection(book, db_path):
    connection, data = book
    scripts = Path(mod.__file__).resolve().parents[1]
    environment = dict(os.environ, PYTHONPATH=str(scripts / "erpclaw-setup" / "lib"))
    result = subprocess.run([
        sys.executable, str(scripts / "db_query.py"), "--action", "add-expense-schedule",
        "--db-path", db_path, "--company-id", data["company_id"],
        "--template-name", "Annual insurance", "--schedule-kind", "prepaid",
        "--amount", "1200.03", "--periods", "12", "--start-date", "2026-01-01",
        "--expense-account-id", data["expense"], "--balance-account-id", data["prepaid"],
    ], capture_output=True, text=True, env=environment, timeout=30)
    assert result.returncode == 0, result.stderr + result.stdout
    reply = json.loads(result.stdout)
    assert reply["total"] == "1200.03" and reply["periods"] == 12
    fresh = get_connection(db_path)
    try:
        templates = rows(fresh, "recurring_journal_template")
        assert len(templates) == 12
        assert sum((Decimal(json.loads(t["lines"])[0]["debit"]) for t in templates),
                   Decimal("0")) == Decimal("1200.03")
        assert rows(fresh, "gl_entry") == []
    finally:
        fresh.close()
