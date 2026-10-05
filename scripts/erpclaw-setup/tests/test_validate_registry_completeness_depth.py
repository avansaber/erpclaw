"""Live-data checks for the validate-registry-completeness action."""

import argparse
from decimal import Decimal
from uuid import uuid4

from erpclaw_lib import seam
from erpclaw_lib.seam import MetaData, Table as SchemaTable
from erpclaw_lib.query import P, Q, Table
from setup_helpers import (
    call_action,
    freeze_snapshot,
    load_db_query,
    open_reader,
    read_one,
    seed_account,
    seed_company,
)


def _run(conn):
    module = load_db_query()
    return call_action(
        module.validate_registry_completeness, conn, argparse.Namespace()
    )


def _register(conn, function, **values):
    module = load_db_query()
    arguments = dict(label="Fixture type", skill_name="erpclaw-setup")
    arguments.update(values)
    result = call_action(getattr(module, function), conn, argparse.Namespace(**arguments))
    assert result["status"] == "ok"


def _unchanged_diagnostic(conn, db_path):
    reader = open_reader(db_path)
    try:
        tables = seam.table_names(db_path)
        before = freeze_snapshot(reader, db_path, tables)
        result = _run(conn)
        assert result["status"] == "ok"
        assert freeze_snapshot(reader, db_path, tables) == before
        return result
    finally:
        reader.close()


def test_registered_custom_account_type_in_use_is_complete(conn, db_path):
    _register(conn, "add_account_type", account_type="fixture_clearing")
    company = seed_company(conn)
    account = seed_account(conn, company, account_type="fixture_clearing")

    result = _unchanged_diagnostic(conn, db_path)

    assert result["complete"] is True
    assert result["unregistered_in_use"] == {}
    assert read_one(conn, "account", ["account_type"], account) == {
        "account_type": "fixture_clearing"
    }


def test_registered_but_inactive_account_type_in_use_is_reported(conn, db_path):
    company = seed_company(conn)
    account = seed_account(conn, company, account_type="bank")
    registry = Table("account_type_registry")
    query = Q.update(registry).set(registry.is_active, P()).where(
        registry.account_type == P()
    )
    conn.execute(query.get_sql(), (0, "bank"))
    conn.commit()

    result = _unchanged_diagnostic(conn, db_path)

    assert result["complete"] is False
    assert result["unregistered_in_use"] == {"account_type": ["bank"]}
    assert read_one(conn, "account", ["account_type"], account)["account_type"] == "bank"


def test_voucher_registration_is_scoped_to_its_target_table(conn, db_path):
    voucher = "fixture_adjustment"
    _register(conn, "add_voucher_type", voucher_type=voucher,
              target_table="stock_ledger_entry")
    company = seed_company(conn)
    account = seed_account(conn, company, account_type="cash")
    entry_id = str(uuid4())
    entry = Table("gl_entry")
    amount = Decimal("500.00")
    query = Q.into(entry).columns(
        "id", "posting_date", "account_id", "debit", "credit",
        "voucher_type", "voucher_id"
    ).insert(P(), P(), P(), P(), P(), P(), P())
    conn.execute(query.get_sql(), (
        entry_id, "2026-10-04", account, str(amount), "0.00",
        voucher, "fixture-document"
    ))
    conn.commit()

    missing = _unchanged_diagnostic(conn, db_path)
    assert missing["complete"] is False
    assert missing["unregistered_in_use"] == {"voucher_type[gl_entry]": [voucher]}

    _register(conn, "add_voucher_type", voucher_type=voucher, target_table="gl_entry")
    complete = _unchanged_diagnostic(conn, db_path)
    assert complete["complete"] is True
    assert complete["unregistered_in_use"] == {}
    stored = read_one(conn, "gl_entry", ["debit", "credit", "voucher_type"], entry_id)
    assert Decimal(stored["debit"]) == amount
    assert Decimal(stored["credit"]) == Decimal("0.00")
    assert stored["voucher_type"] == voucher


def test_missing_optional_asset_table_is_tolerated_without_recreating_it(conn, db_path):
    engine = seam.get_engine(db_path)
    SchemaTable("asset", MetaData(), autoload_with=engine).drop(engine)
    assert seam.table_exists("asset", db_path) is False

    result = _unchanged_diagnostic(conn, db_path)

    assert result["complete"] is True
    assert result["unregistered_in_use"] == {}
    assert seam.table_exists("asset", db_path) is False
