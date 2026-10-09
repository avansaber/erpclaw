"""Contract positions from posted books, with separate receivables."""
from pathlib import Path
from uuid import uuid4

import pytest

from advacct_helpers import (build_advacct_env, call_action, load_db_query, ns,
                             seed_recognition_ledger, _ConnWrapper)
from erpclaw_lib.db import get_connection
from erpclaw_lib.gl_posting import insert_gl_entries, reverse_gl_entries
from erpclaw_lib.query import P, Q, Table

mod = load_db_query()


def _insert(conn, table, values):
    query = Q.into(Table(table)).columns(*values).insert(*[P() for _ in values])
    conn.execute(query.get_sql(), tuple(values.values()))
    conn.commit()


@pytest.fixture
def book(db_path):
    conn = _ConnWrapper(get_connection(db_path))
    env = build_advacct_env(conn)
    ledger = seed_recognition_ledger(conn, env["company_id"])
    env.update(ledger)
    for role in ("asset", "receivable"):
        env[role] = str(uuid4())
        _insert(conn, "account", {"id": env[role], "name": role, "root_type": "asset",
                "account_type": "temporary", "company_id": env["company_id"],
                "currency": "USD", "is_group": 0})
    for role in ("first", "second"):
        env[role] = str(uuid4())
        _insert(conn, "project", {"id": env[role], "project_name": f"Contract {role}",
                                  "company_id": env["company_id"]})
    yield conn, env
    conn.close()


def _post(conn, env, project, account_id, debit="0.00", credit="0.00", day="2026-01-31"):
    voucher = str(uuid4())
    insert_gl_entries(conn, [
        {"account_id": account_id, "debit": debit, "credit": credit,
         "project_id": project, "currency": "USD", "exchange_rate": "1"},
        {"account_id": env["revenue_account_id"], "debit": credit, "credit": debit,
         "cost_center_id": env["cost_center_id"], "project_id": project,
         "currency": "USD", "exchange_rate": "1"}],
        voucher_type="journal_entry", voucher_id=voucher, posting_date=day,
        company_id=env["company_id"])
    conn.commit()
    return voucher


def _args(env, **changes):
    values = {"company_id": env["company_id"], "as_of_date": "2026-01-31",
              "contract_asset_account_id": env["asset"],
              "contract_liability_account_id": env["deferred_revenue_account_id"],
              "receivable_account_id": env["receivable"]}
    values.update(changes)
    return values


def _snapshot(conn):
    return {name: sorted(repr(dict(row)) for row in conn.execute(
        Q.from_(Table(name)).select(Table(name).star).get_sql()).fetchall())
        for name in ("gl_entry", "gl_chain_head", "account", "project", "audit_log")}


def test_contracts_are_net_each_but_presented_gross_across_contracts(book):
    conn, env = book
    _post(conn, env, env["first"], env["asset"], debit="900.25")
    _post(conn, env, env["first"], env["deferred_revenue_account_id"], credit="100.00")
    _post(conn, env, env["second"], env["asset"], debit="100.00")
    _post(conn, env, env["second"], env["deferred_revenue_account_id"], credit="600.10")
    _post(conn, env, env["first"], env["receivable"], debit="250.05")
    before = _snapshot(conn)
    result = call_action(mod.contract_balance_report, conn, ns(**_args(env)))
    assert result["status"] == "ok", result
    assert result["currency"] == "USD"
    assert result["gross_contract_assets"] == "800.25"
    assert result["gross_contract_liabilities"] == "500.10"
    assert result["receivables"] == "250.05"
    by_project = {row["project_id"]: row for row in result["rows"]}
    assert by_project[env["first"]]["net_contract_position"] == "800.25"
    assert by_project[env["second"]]["net_contract_position"] == "-500.10"
    assert by_project[env["first"]]["receivable"] == "250.05"
    assert _snapshot(conn) == before


def test_date_cancelled_and_foreign_company_balances_do_not_enter_report(book):
    conn, env = book
    _post(conn, env, env["first"], env["asset"], debit="50.01")
    _post(conn, env, env["first"], env["asset"], debit="800.00", day="2026-02-01")
    cancelled = _post(conn, env, env["first"], env["asset"], debit="400.00")
    reverse_gl_entries(conn, "journal_entry", cancelled, "2026-01-31")
    conn.commit()
    foreign = seed_recognition_ledger(conn, env["company2_id"])
    _post(conn, {**env, **foreign, "company_id": env["company2_id"]}, None,
          foreign["deferred_revenue_account_id"], credit="9000.00")
    result = call_action(mod.contract_balance_report, conn, ns(**_args(env)))
    assert result["status"] == "ok", result
    assert result["gross_contract_assets"] == "50.01"
    assert result["gross_contract_liabilities"] == "0.00"


@pytest.mark.parametrize("changes", [
    {"company_id": "missing"}, {"as_of_date": "2026-02-30"},
    {"as_of_date": "20260131"}, {"contract_asset_account_id": None},
    {"contract_asset_account_id": "missing"}, {"contract_liability_account_id": None},
])
def test_bad_scope_and_mapping_refused_without_writes(book, changes):
    conn, env = book
    before = _snapshot(conn)
    result = call_action(mod.contract_balance_report, conn, ns(**_args(env, **changes)))
    assert result["status"] == "error", result
    assert _snapshot(conn) == before


def test_wrong_account_roots_duplicate_roles_and_foreign_accounts_refused(book):
    conn, env = book
    for changes in ({"contract_asset_account_id": env["revenue_account_id"]},
                    {"contract_liability_account_id": env["asset"]},
                    {"receivable_account_id": env["asset"]},
                    {"company_id": env["company2_id"]}):
        before = _snapshot(conn)
        result = call_action(mod.contract_balance_report, conn, ns(**_args(env, **changes)))
        assert result["status"] == "error", result
        assert _snapshot(conn) == before


def test_missing_contract_tag_is_an_explicit_refusal(book):
    conn, env = book
    _post(conn, env, None, env["asset"], debit="50.00")
    before = _snapshot(conn)
    result = call_action(mod.contract_balance_report, conn, ns(**_args(env)))
    assert result["status"] == "error"
    assert "project tag" in result["message"]
    assert _snapshot(conn) == before


def test_empty_books_and_optional_receivable_mapping(book):
    conn, env = book
    result = call_action(mod.contract_balance_report, conn,
                         ns(**_args(env, receivable_account_id=None)))
    assert result["status"] == "ok" and result["rows"] == []
    assert result["gross_contract_assets"] == result["gross_contract_liabilities"] == "0.00"


def test_actual_seeded_read_only_sweep(book, tmp_path, monkeypatch):
    conn, env = book
    _post(conn, env, env["first"], env["asset"], debit="100.10")
    snapshot = tmp_path / "contract-snapshot.sqlite"
    conn.execute("VACUUM INTO ?", (str(snapshot),))
    conn.close()
    root = Path(__file__).resolve().parents[5]
    monkeypatch.syspath_prepend(str(root / "testing"))
    import readonly_sweep
    argv = readonly_sweep._child_argv("contract-balance-report", _args(env))
    result = readonly_sweep._dispatch_once(argv, readonly_sweep._child_env, str(snapshot))
    assert result["status"] == "ok" and result["findings"] == [], result
