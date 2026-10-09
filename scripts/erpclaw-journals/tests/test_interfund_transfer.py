"""Reciprocal interfund drafts reuse the journal submit and cancel paths."""
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
from decimal import Decimal

import pytest

from journals_helpers import build_journals_env, call_action, load_db_query, ns, seed_account
from erpclaw_lib.db import get_connection
from erpclaw_lib.query import Q, P, Table, Order, insert_row

mod = load_db_query()


def read_rows(connection, table):
    t = Table(table)
    return [dict(row) for row in connection.execute(
        Q.from_(t).select(t.star).orderby(t.id, order=Order.asc).get_sql(), ()).fetchall()]


@pytest.fixture
def book(db_path):
    connection = get_connection(db_path)
    data = build_journals_env(connection)
    data["target_cash"] = seed_account(connection, data["company_id"], "Target bank", "asset", "bank", "1001")
    data["due_from"] = seed_account(connection, data["company_id"], "Due from other fund", "asset", None, "1300")
    data["due_to"] = seed_account(connection, data["company_id"], "Due to other fund", "liability", None, "2300")
    values = {"id": str(uuid.uuid4()), "key": "fund", "label": "Fund", "data_type": "enum",
              "allowed_values_json": json.dumps(["GENERAL", "RESTRICTED"]), "is_active": 1}
    sql, columns = insert_row("dimension_registry", {key: P() for key in values})
    connection.execute(sql, tuple(values[key] for key in columns))
    connection.commit()
    yield connection, data
    connection.close()


def args(data, **changes):
    values = dict(company_id=data["company_id"], posting_date="2026-06-20", fund_dimension="fund",
                  from_fund="GENERAL", to_fund="RESTRICTED", amount="500.25",
                  source_cash_account_id=data["cash"], target_cash_account_id=data["target_cash"],
                  due_from_account_id=data["due_from"], due_to_account_id=data["due_to"],
                  dimensions=json.dumps({"department": "Finance"}), dimension_key=None,
                  dimension_value=None, lines=None, entry_type=None, cwip_asset_id=None,
                  remark="Working capital")
    values.update(changes)
    return ns(**values)


def snapshot(connection):
    return {name: read_rows(connection, name) for name in (
        "journal_entry", "journal_entry_line", "gl_entry", "audit_log", "naming_series")}


def balances(rows):
    result = {}
    for row in rows:
        key = json.loads(row["dimensions_json"])["fund"]
        result[key] = result.get(key, Decimal("0")) + Decimal(row["debit"]) - Decimal(row["credit"])
    return result


def test_reciprocal_draft_and_existing_submit_cancel(book):
    connection, data = book
    result = call_action(mod.add_interfund_transfer, connection, args(data))
    assert result["status"] == "ok", result
    identifier = result["journal_entry_id"]
    entry = read_rows(connection, "journal_entry")[0]
    assert entry["status"] == "draft"
    assert Decimal(entry["total_debit"]) == Decimal("1000.50")
    assert Decimal(entry["total_credit"]) == Decimal("1000.50")
    lines = read_rows(connection, "journal_entry_line")
    assert len(lines) == 4
    assert balances(lines) == {"GENERAL": Decimal("0"), "RESTRICTED": Decimal("0")}
    by_account = {line["account_id"]: line for line in lines}
    assert Decimal(by_account[data["due_from"]]["debit"]) == Decimal("500.25")
    assert Decimal(by_account[data["due_to"]]["credit"]) == Decimal("500.25")
    assert read_rows(connection, "gl_entry") == []
    submitted = call_action(mod.submit_journal_entry, connection, ns(journal_entry_id=identifier))
    assert submitted["status"] == "ok", submitted
    posted = read_rows(connection, "gl_entry")
    assert len(posted) == 4
    assert balances(posted) == {"GENERAL": Decimal("0"), "RESTRICTED": Decimal("0")}
    assert all(json.loads(row["dimensions_json"])["department"] == "Finance" for row in posted)
    cancelled = call_action(mod.cancel_journal_entry, connection, ns(journal_entry_id=identifier))
    assert cancelled["status"] == "ok", cancelled
    assert balances(read_rows(connection, "gl_entry")) == {
        "GENERAL": Decimal("0"), "RESTRICTED": Decimal("0")}
    assert read_rows(connection, "journal_entry")[0]["status"] == "cancelled"


@pytest.mark.parametrize("changes", [
    {"amount": "NaN"}, {"amount": "1.001"}, {"amount": "1e2"}, {"amount": "0"},
    {"amount": 10.0}, {"from_fund": "RESTRICTED"}, {"to_fund": "UNKNOWN"},
    {"from_fund": " GENERAL"}, {"to_fund": ""}, {"fund_dimension": "department"},
    {"posting_date": "2026-02-30"}, {"posting_date": "20260620"},
    {"lines": "[]"}, {"entry_type": "opening"}, {"cwip_asset_id": "missing"},
    {"dimensions": '{"fund":"GENERAL"}'}, {"due_from_account_id": "missing"},
    {"dimensions": '{"unknown":"X"}'},
])
def test_refusal_preserves_books_and_naming(book, changes):
    connection, data = book
    before = snapshot(connection)
    result = call_action(mod.add_interfund_transfer, connection, args(data, **changes))
    assert result["status"] == "error", result
    assert snapshot(connection) == before


def test_foreign_account_and_wrong_roots_refuse(book):
    connection, data = book
    other = build_journals_env(connection)
    for changes in ({"source_cash_account_id": other["cash"]},
                    {"target_cash_account_id": data["expense"]},
                    {"due_from_account_id": data["cash"]},
                    {"due_to_account_id": data["due_from"]}):
        before = snapshot(connection)
        reply = call_action(mod.add_interfund_transfer, connection, args(data, **changes))
        assert reply["status"] == "error", reply
        assert snapshot(connection) == before


def test_late_draft_failure_rolls_back_lines_audit_and_names(book, monkeypatch):
    connection, data = book
    before = snapshot(connection)

    def fail_audit(*values, **kwargs):
        raise ValueError("planted draft audit failure")

    monkeypatch.setattr(mod, "audit", fail_audit)
    with pytest.raises(ValueError, match="planted draft audit failure"):
        mod.add_interfund_transfer(connection, args(data))
    assert snapshot(connection) == before


def test_actual_root_route_writes_only_a_draft(book, db_path):
    connection, data = book
    scripts = Path(mod.__file__).resolve().parents[1]
    env = dict(os.environ, PYTHONPATH=str(scripts / "erpclaw-setup" / "lib"))
    result = subprocess.run([
        sys.executable, str(scripts / "db_query.py"), "--action", "add-interfund-transfer",
        "--db-path", db_path, "--company-id", data["company_id"], "--posting-date", "2026-06-20",
        "--from-fund", "GENERAL", "--to-fund", "RESTRICTED", "--amount", "500.25",
        "--source-cash-account-id", data["cash"], "--target-cash-account-id", data["target_cash"],
        "--due-from-account-id", data["due_from"], "--due-to-account-id", data["due_to"],
    ], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["journal_entry_id"]
    fresh = get_connection(db_path)
    try:
        assert read_rows(fresh, "journal_entry")[0]["status"] == "draft"
        assert balances(read_rows(fresh, "journal_entry_line")) == {
            "GENERAL": Decimal("0"), "RESTRICTED": Decimal("0")}
        assert read_rows(fresh, "gl_entry") == []
    finally:
        fresh.close()
