"""Two-class financial statements from explicitly classified posted books."""
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
    "nonprofit_reports", Path(__file__).resolve().parents[1] / "db_query.py")
REPORTS = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(REPORTS)
WITHOUT = "without_donor_restrictions"
WITH = "with_donor_restrictions"


def insert(conn, table, **values):
    conn.execute(Q.into(Table(table)).columns(*values).insert(
        *(P() for _ in values)).get_sql(), tuple(values.values()))


def account(conn, company, name, root, kind):
    aid = str(uuid4())
    insert(conn, "account", id=aid, company_id=company, name=name,
           account_number=aid, root_type=root, account_type=kind,
           balance_direction="debit_normal" if root in ("asset", "expense")
           else "credit_normal", depth=0)
    return aid


def pair(conn, debit, credit, amount, tag=WITHOUT, date="2026-02-01",
         voucher_type="journal_entry", cancelled=0):
    voucher = str(uuid4())
    for aid, dr, cr in ((debit, amount, "0"), (credit, "0", amount)):
        insert(conn, "gl_entry", id=str(uuid4()), account_id=aid,
               posting_date=date, debit=dr, credit=cr, debit_base=dr,
               credit_base=cr, currency="USD", exchange_rate="1",
               voucher_type=voucher_type, voucher_id=voucher,
               is_cancelled=cancelled,
               dimensions_json=json.dumps({"net_asset_class": tag}) if tag else "{}")
    return voucher


@pytest.fixture
def book(conn):
    company = str(uuid4())
    insert(conn, "company", id=company, name="Community Books", abbr="CB")
    insert(conn, "dimension_registry", id=str(uuid4()), key="net_asset_class",
           label="Net asset class", data_type="enum",
           allowed_values_json=json.dumps([WITHOUT, WITH]))
    ids = {name: account(conn, company, name, root, kind)
           for name, root, kind in (("cash", "asset", "bank"),
                                   ("income", "income", "revenue"),
                                   ("expense", "expense", "expense"),
                                   ("equity", "equity", "equity"),
                                   ("equipment", "asset", "fixed_asset"))}
    pair(conn, ids["cash"], ids["equity"], "1000.00", date="2025-12-31")
    pair(conn, ids["cash"], ids["income"], "1250.10")
    pair(conn, ids["cash"], ids["income"], "1500.20", tag=WITH)
    voucher = str(uuid4())
    for tag, dr, cr in ((WITH, "500.05", "0"), (WITHOUT, "0", "500.05")):
        insert(conn, "gl_entry", id=str(uuid4()), account_id=ids["equity"],
               posting_date="2026-03-01", debit=dr, credit=cr, debit_base=dr,
               credit_base=cr, voucher_type="net_asset_release", voucher_id=voucher,
               dimensions_json=json.dumps({"net_asset_class": tag}))
    pair(conn, ids["expense"], ids["cash"], "250.15")
    pair(conn, ids["equipment"], ids["cash"], "100.10")
    # Closing transfers leave activity and cash unchanged.
    pair(conn, ids["income"], ids["equity"], "1250.10",
         voucher_type="period_closing")
    pair(conn, ids["cash"], ids["income"], "999.99", tag=None, cancelled=1)
    pair(conn, ids["cash"], ids["income"], "999.99", tag=None, date="2027-01-01")
    foreign = str(uuid4())
    insert(conn, "company", id=foreign, name="Other Books", abbr="OB")
    pair(conn, account(conn, foreign, "foreign cash", "asset", "bank"),
         account(conn, foreign, "foreign income", "income", "revenue"),
         "999999.99", tag=None)
    conn.commit()
    return {"company": company, **ids}


def run(conn, book, **overrides):
    args = dict(company_id=book["company"], from_date="2026-01-01",
                to_date="2026-12-31", net_asset_dimension="net_asset_class",
                release_voucher_types="net_asset_release",
                cash_flow_account_map=json.dumps({book["income"]: "operating",
                    book["expense"]: "operating", book["equipment"]: "investing"}))
    args.update(overrides)
    return call_action(REPORTS.nonprofit_statement_set, conn, ns(**args))


def snapshot(conn):
    return {name: [tuple(row) for row in conn.execute(
        Q.from_(Table(name)).select("*").orderby(Table(name).id).get_sql()).fetchall()]
        for name in ("company", "account", "gl_entry", "dimension_registry")}


def test_exact_statement_set_and_read_only_company_scope(conn, book):
    before = snapshot(conn)
    result = run(conn, book)
    assert result["status"] == "ok", result
    assert result["financial_position"]["totals"] == {
        "assets": "3500.15", "liabilities": "0.00", "net_assets": "3500.15"}
    assert result["activities"][WITHOUT] == {
        "opening_net_assets": "1000.00", "revenue": "1250.10",
        "releases": "500.05", "expenses": "250.15",
        "change_in_net_assets": "1500.00", "closing_net_assets": "2500.00"}
    assert result["activities"][WITH]["closing_net_assets"] == "1000.15"
    assert result["activities"][WITH]["releases"] == "-500.05"
    assert result["cash_flow"] == {
        "operating": "2500.15", "investing": "-100.10", "financing": "0.00",
        "opening_cash": "1000.00", "closing_cash": "3400.05",
        "net_change": "2400.05", "change_in_net_assets": "2500.15",
        "operating_reconciliation_adjustment": "0.00"}
    assert snapshot(conn) == before


@pytest.mark.parametrize("tag", [None, "unrestricted"])
def test_refuses_unclassified_or_unknown_classes_without_writes(conn, book, tag):
    pair(conn, book["cash"], book["income"], "0.01", tag=tag)
    conn.commit()
    before = snapshot(conn)
    result = run(conn, book)
    assert result["status"] == "error"
    assert "class tag" in result["message"]
    assert snapshot(conn) == before


@pytest.mark.parametrize("overrides, message", [
    ({"net_asset_dimension": "undeclared"}, "registered and active"),
    ({"cash_flow_account_map": "{}"}, "cash-flow category"),
    ({"from_date": "2026-12-31", "to_date": "2026-01-01"}, "ascending"),
])
def test_required_configuration_refuses(conn, book, overrides, message):
    before = snapshot(conn)
    result = run(conn, book, **overrides)
    assert result["status"] == "error"
    assert message in result["message"]
    assert snapshot(conn) == before


def test_restricted_expense_refuses(conn, book):
    pair(conn, book["expense"], book["cash"], "1.01", tag=WITH)
    conn.commit()
    result = run(conn, book)
    assert result["status"] == "error"
    assert "Expenses must reduce" in result["message"]


def test_non_cash_category_transfer_is_not_reported_as_cash_flow(conn, book):
    pair(conn, book["equipment"], book["income"], "10.00")
    conn.commit()
    result = run(conn, book)
    assert result["status"] == "error"
    assert "Non-cash vouchers" in result["message"]


def test_exact_cents_at_large_amounts(conn, book):
    pair(conn, book["cash"], book["income"], "100000000000000.07")
    conn.commit()
    result = run(conn, book)
    assert result["status"] == "ok", result
    assert result["activities"][WITHOUT]["revenue"] == "100000000001250.17"
    assert result["financial_position"]["totals"]["net_assets"] == "100000000003500.22"
    assert result["cash_flow"]["closing_cash"] == "100000000003400.12"


def test_report_succeeds_with_read_only_storage(conn, db_path, book, monkeypatch):
    if get_dialect() != "sqlite":
        pytest.skip("Filesystem read-only fixture uses SQLite")
    before = snapshot(conn)
    monkeypatch.setenv("ERPCLAW_DB_READONLY", "1")
    reader = get_connection(db_path)
    try:
        result = run(reader, book)
        assert result["status"] == "ok", result
    finally:
        reader.close()
    assert snapshot(conn) == before


def test_registry_must_explicitly_declare_both_classes(conn, book):
    table = Table("dimension_registry")
    conn.execute(Q.update(table).set(table.allowed_values_json, P()).where(
        table.key == P()).get_sql(), (json.dumps([WITHOUT]), "net_asset_class"))
    conn.commit()
    result = run(conn, book)
    assert result["status"] == "error"
    assert "declare exactly" in result["message"]


def test_cli_routes_and_parses_statement_inputs(conn, db_path, book, monkeypatch, capsys):
    mapping = json.dumps({book["income"]: "operating", book["expense"]: "operating",
                          book["equipment"]: "investing"})
    monkeypatch.setattr(sys, "argv", ["reports", "--action", "nonprofit-statement-set",
        "--db-path", db_path, "--company-id", book["company"],
        "--from-date", "2026-01-01", "--to-date", "2026-12-31",
        "--net-asset-dimension", "net_asset_class", "--cash-flow-account-map", mapping,
        "--release-voucher-types", "net_asset_release"])
    before = snapshot(conn)
    with pytest.raises(SystemExit) as stopped:
        REPORTS.main()
    assert stopped.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok", result
    assert result["company_id"] == book["company"]
    assert result["cash_flow"]["closing_cash"] == "3400.05"
    assert snapshot(conn) == before


def test_operating_reconciliation_excludes_uncollected_revenue(conn, book):
    receivable = account(conn, book["company"], "Receivable", "asset", "receivable")
    pair(conn, receivable, book["income"], "100.00")
    pair(conn, book["cash"], receivable, "60.00")
    conn.commit()
    mapping = json.dumps({book["income"]: "operating", book["expense"]: "operating",
                          book["equipment"]: "investing", receivable: "operating"})
    result = run(conn, book, cash_flow_account_map=mapping)
    assert result["status"] == "ok", result
    assert result["cash_flow"]["operating"] == "2560.15"
    assert result["cash_flow"]["change_in_net_assets"] == "2600.15"
    assert result["cash_flow"]["operating_reconciliation_adjustment"] == "-40.00"
    assert result["cash_flow"]["closing_cash"] == "3460.05"
