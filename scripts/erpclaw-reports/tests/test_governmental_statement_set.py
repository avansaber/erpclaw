"""Exact fund and government-wide reconciliations over classified posted books."""
import importlib.util
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from erpclaw_lib.query import Q, Table, P
from erpclaw_lib.db import get_connection, get_dialect
from payments_helpers import call_action, ns

_SPEC = importlib.util.spec_from_file_location(
    "government_reports", Path(__file__).resolve().parents[1] / "db_query.py")
REPORTS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(REPORTS)
CLASSES = ("net_investment_in_capital_assets", "restricted_expendable",
           "restricted_nonexpendable", "unrestricted")


def insert(conn, table, **values):
    conn.execute(Q.into(Table(table)).columns(*values).insert(
        *(P() for _ in values)).get_sql(), tuple(values.values()))


def account(conn, company, name, root):
    aid = str(uuid4())
    insert(conn, "account", id=aid, company_id=company, name=name,
           account_number=aid, root_type=root, depth=0,
           balance_direction="debit_normal" if root in ("asset", "expense")
           else "credit_normal")
    return aid


def pair(conn, debit, credit, amount, date="2026-02-01", fund="general",
         kind="journal_entry", classification="unrestricted", cancelled=0):
    voucher = str(uuid4())
    for aid, dr, cr in ((debit, amount, "0"), (credit, "0", amount)):
        insert(conn, "gl_entry", id=str(uuid4()), account_id=aid,
               posting_date=date, debit=dr, credit=cr, debit_base=dr,
               credit_base=cr, currency="USD", exchange_rate="1",
               voucher_type=kind, voucher_id=voucher, is_cancelled=cancelled,
               dimensions_json=json.dumps({"fund": fund, "position_class": classification}))
    return voucher


@pytest.fixture
def book(conn):
    company = str(uuid4())
    insert(conn, "company", id=company, name="Town Books", abbr="TB")
    for key, values in (("fund", ["general", "capital"]), ("position_class", CLASSES)):
        insert(conn, "dimension_registry", id=str(uuid4()), key=key, label=key,
               data_type="enum", allowed_values_json=json.dumps(values))
    roots = {"cash": "asset", "capital": "asset", "accumulated": "asset",
             "outflows": "asset", "payable": "liability", "loan": "liability",
             "inflows": "liability", "equity": "equity", "revenue": "income",
             "expense": "expense"}
    ids = {name: account(conn, company, name, root) for name, root in roots.items()}
    pair(conn, ids["cash"], ids["equity"], "1000.00", date="2025-12-31")
    pair(conn, ids["cash"], ids["revenue"], "500.10")
    pair(conn, ids["capital"], ids["cash"], "100.05")
    pair(conn, ids["cash"], ids["loan"], "200.20")
    pair(conn, ids["cash"], ids["inflows"], "50.00")
    pair(conn, ids["outflows"], ids["loan"], "100.00", kind="government_conversion")
    pair(conn, ids["expense"], ids["accumulated"], "10.01", kind="government_conversion")
    pair(conn, ids["cash"], ids["equity"], "20.00", fund="capital", date="2025-12-31",
         classification="restricted_nonexpendable")
    pair(conn, ids["cash"], ids["revenue"], "3.03", fund="capital",
         classification="restricted_expendable")
    # Closing, cancelled, future and foreign legs must not invent current activity.
    pair(conn, ids["revenue"], ids["equity"], "500.10", kind="period_closing")
    pair(conn, ids["cash"], ids["revenue"], "999.99", fund=None, cancelled=1)
    pair(conn, ids["cash"], ids["revenue"], "999.99", fund=None, date="2027-01-01")
    foreign = str(uuid4())
    insert(conn, "company", id=foreign, name="Other Town", abbr="OT")
    pair(conn, account(conn, foreign, "foreign cash", "asset"),
         account(conn, foreign, "foreign income", "income"), "999999.99", fund=None)
    conn.commit()
    return {"company": company, **ids}


def arguments(book):
    roles = {"cash": "current_assets", "capital": "capital_assets",
             "accumulated": "capital_assets", "outflows": "deferred_outflows",
             "payable": "current_liabilities", "loan": "long_term_liabilities",
             "inflows": "deferred_inflows", "equity": "net_position",
             "revenue": "revenues", "expense": "expenses"}
    return dict(company_id=book["company"], from_date="2026-01-01", to_date="2026-12-31",
                fund_dimension="fund", net_position_dimension="position_class",
                fund_basis_map=json.dumps({"general": "modified_accrual", "capital": "modified_accrual"}),
                government_account_map=json.dumps({book[name]: role for name, role in roles.items()}),
                conversion_voucher_types=json.dumps(["government_conversion"]))


def run(conn, book, **overrides):
    args = arguments(book)
    args.update(overrides)
    return call_action(REPORTS.governmental_statement_set, conn, ns(**args))


def snapshot(conn):
    return {name: [tuple(row) for row in conn.execute(
        Q.from_(Table(name)).select("*").orderby(Table(name).id).get_sql()).fetchall()]
        for name in ("company", "account", "gl_entry", "dimension_registry")}


def test_exact_funds_net_position_and_reconciliations_without_writes(conn, book):
    before = snapshot(conn)
    result = run(conn, book)
    assert result["status"] == "ok", result
    assert result["fund_statements"]["general"] == {
        "basis": "modified_accrual", "opening_fund_balance": "1000.00",
        "closing_fund_balance": "1600.25", "revenues": "500.10",
        "expenditures": "100.05", "other_financing_sources_and_uses": "200.20",
        "change_in_fund_balance": "600.25"}
    assert result["fund_statements"]["capital"]["closing_fund_balance"] == "23.03"
    position = result["statement_of_net_position"]
    assert position == {"current_assets": "1673.28", "capital_assets": "90.04",
        "deferred_outflows": "100.00", "current_liabilities": "0.00",
        "long_term_liabilities": "300.20", "deferred_inflows": "50.00",
        "net_position": "1513.12", "classes": {
            CLASSES[0]: "0.00", CLASSES[1]: "3.03", CLASSES[2]: "20.00", CLASSES[3]: "1490.09"}}
    assert result["statement_of_activities"]["unrestricted"] == {
        "opening_net_position": "1000.00", "revenues": "500.10", "expenses": "10.01",
        "other_changes": "0.00", "closing_net_position": "1490.09",
        "change_in_net_position": "490.09"}
    assert result["reconciliation"]["position"] == {
        "fund_balances": "1623.28", "capital_assets": "90.04", "deferred_outflows": "100.00",
        "long_term_liabilities": "-300.20", "conversion_current_resources": "0.00",
        "government_wide_net_position": "1513.12"}
    assert result["reconciliation"]["activity"]["government_wide_change"] == "493.12"
    assert "no automatic recognition" in result["scope"]
    assert snapshot(conn) == before


@pytest.mark.parametrize("overrides, message", [
    ({"fund_dimension": "missing"}, "registered active"),
    ({"net_position_dimension": "fund"}, "Distinct"),
    ({"fund_basis_map": "{}"}, "every declared fund"),
    ({"fund_basis_map": '{"general":"full_accrual","capital":"modified_accrual"}'}, "every declared fund"),
    ({"government_account_map": "{}"}, "explicit government statement role"),
    ({"government_account_map": '{"unknown-account":"capital_assets"}'}, "belong to the company"),
    ({"government_account_map": '{"unknown-account":{}}'}, "supported statement roles"),
    ({"conversion_voucher_types": None}, "explicit JSON list"),
    ({"conversion_voucher_types": '["period_closing"]'}, "non-closing"),
    ({"from_date": "2027-01-01"}, "ascending"),
])
def test_missing_or_unknown_configuration_refuses_without_writes(conn, book, overrides, message):
    before = snapshot(conn)
    result = run(conn, book, **overrides)
    assert result["status"] == "error", result
    assert message in result["message"]
    assert snapshot(conn) == before


@pytest.mark.parametrize("fund, classification", [(None, "unrestricted"),
    ("unknown", "unrestricted"), ([], "unrestricted"), ("general", None), ("general", "unknown")])
def test_untagged_or_unknown_ledger_classes_refuse(conn, book, fund, classification):
    pair(conn, book["cash"], book["revenue"], "0.01", fund=fund, classification=classification)
    conn.commit()
    result = run(conn, book)
    assert result["status"] == "error", result
    assert "recognised fund and net-position" in result["message"]


def test_wrong_account_root_refuses(conn, book):
    args = arguments(book)
    mapping = json.loads(args["government_account_map"])
    mapping[book["cash"]] = "long_term_liabilities"
    result = run(conn, book, government_account_map=json.dumps(mapping))
    assert result["status"] == "error"
    assert "compatible account roots" in result["message"]


def test_incomplete_registered_net_position_classes_refuse(conn, book):
    table = Table("dimension_registry")
    conn.execute(Q.update(table).set(table.allowed_values_json, P()).where(
        table.key == P()).get_sql(), (json.dumps(["unrestricted"]), "position_class"))
    conn.commit()
    result = run(conn, book)
    assert result["status"] == "error"
    assert "four supported classes" in result["message"]


def test_cross_fund_partial_voucher_refuses_even_when_company_balances(conn, book):
    voucher = pair(conn, book["cash"], book["revenue"], "1.01")
    table = Table("gl_entry")
    conn.execute(Q.update(table).set(table.dimensions_json, P()).where(
        table.voucher_id == P()).where(table.account_id == P()).get_sql(),
        (json.dumps({"fund": "capital", "position_class": "unrestricted"}), voucher, book["revenue"]))
    conn.commit()
    result = run(conn, book)
    assert result["status"] == "error"
    assert "balance within its declared fund" in result["message"]


def test_government_only_current_resource_conversion_is_reconciled(conn, book):
    pair(conn, book["cash"], book["revenue"], "1.23", kind="government_conversion")
    conn.commit()
    result = run(conn, book)
    assert result["status"] == "ok", result
    assert result["fund_statements"]["general"]["closing_fund_balance"] == "1600.25"
    assert result["reconciliation"]["position"]["conversion_current_resources"] == "1.23"
    assert result["reconciliation"]["activity"]["conversion_current_resource_change"] == "1.23"
    assert result["statement_of_net_position"]["net_position"] == "1514.35"


def test_exact_large_cents(conn, book):
    pair(conn, book["cash"], book["revenue"], "100000000000000.07")
    conn.commit()
    result = run(conn, book)
    assert result["status"] == "ok", result
    assert result["statement_of_net_position"]["net_position"] == "100000000001513.19"
    assert result["fund_statements"]["general"]["revenues"] == "100000000000500.17"


def test_explicit_net_position_class_transfer_is_not_revenue(conn, book):
    voucher = pair(conn, book["equity"], book["equity"], "90.04",
                   kind="government_conversion")
    table = Table("gl_entry")
    conn.execute(Q.update(table).set(table.dimensions_json, P()).where(
        table.voucher_id == P()).where(table.credit_base == P()).get_sql(),
        (json.dumps({"fund": "general", "position_class": CLASSES[0]}), voucher, "90.04"))
    conn.commit()
    result = run(conn, book)
    assert result["status"] == "ok", result
    classes = result["statement_of_net_position"]["classes"]
    assert classes[CLASSES[0]] == "90.04"
    assert classes["unrestricted"] == "1400.05"
    assert result["statement_of_activities"][CLASSES[0]]["other_changes"] == "90.04"
    assert result["statement_of_activities"][CLASSES[0]]["revenues"] == "0.00"
    assert result["reconciliation"]["activity"]["government_wide_change"] == "493.12"


@pytest.mark.parametrize("bad_amount", ["NaN", "Infinity", "not-money"])
def test_invalid_ledger_amount_refuses(conn, book, bad_amount):
    table = Table("gl_entry")
    conn.execute(Q.update(table).set(table.debit_base, P()).where(
        table.account_id == P()).where(table.is_cancelled == 0).get_sql(),
        (bad_amount, book["cash"]))
    conn.commit()
    result = run(conn, book)
    assert result["status"] == "error", result
    assert "finite non-negative exact decimals" in result["message"]


def test_read_only_storage(conn, db_path, book, monkeypatch):
    if get_dialect() != "sqlite":
        pytest.skip("Filesystem read-only fixture uses SQLite")
    monkeypatch.setenv("ERPCLAW_DB_READONLY", "1")
    reader = get_connection(db_path)
    try:
        assert run(reader, book)["status"] == "ok"
    finally:
        reader.close()


def test_cli_parses_and_dispatches(conn, db_path, book, monkeypatch, capsys):
    argv = ["reports", "--action", "governmental-statement-set", "--db-path", db_path]
    for key, value in arguments(book).items():
        argv.extend(["--" + key.replace("_", "-"), value])
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as stopped:
        REPORTS.main()
    assert stopped.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok", result
    assert result["statement_of_net_position"]["net_position"] == "1513.12"
