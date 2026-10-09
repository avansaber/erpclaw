"""Barcode drafts preserve stock scope and use normal ledger submission."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from inventory_helpers import call_action, ns, seed_company, seed_warehouse, seed_account, load_db_query
from erpclaw_lib.query import Q, P, Table
from erpclaw_lib import seam
from erpclaw_lib.gl_invariants import check_gl_invariants
from erpclaw_lib.db import get_connection


@pytest.fixture(scope="module")
def mod():
    return load_db_query()


def args(env, **changes):
    values = dict(company_id=env["company_id"], posting_date="2026-06-15",
                  items=None, scans=None, dimensions=None, dimension_key=None,
                  dimension_value=None, item_id=env["item1"], barcode="WIDGET-A",
                  entry_type="receive")
    values.update(changes)
    return ns(**values)


def register(mod, conn, env, **changes):
    result = call_action(mod.add_item_barcode, conn, args(env, **changes))
    assert result["status"] == "ok", result
    return result


def snapshot(conn):
    names = ("item_barcode", "stock_entry", "stock_entry_item",
             "stock_reconciliation", "stock_reconciliation_item", "naming_series",
             "stock_ledger_entry", "gl_entry", "audit_log")
    result = {}
    for name in names:
        table = Table(name)
        result[name] = sorted(tuple(row) for row in conn.execute(
            Q.from_(table).select(table.star).get_sql()).fetchall())
    return result


def receive(env):
    return {"barcode": "WIDGET-A", "qty": "2.50", "rate": "20.00",
            "to_warehouse_id": env["warehouse"]}


def test_barcode_mapping_is_company_scoped(mod, conn, env):
    register(mod, conn, env)
    other = seed_company(conn, "Other Co", "OC")
    result = register(mod, conn, env, company_id=other, item_id=env["item2"])
    assert result["company_id"] == other
    before = snapshot(conn)
    duplicate = call_action(mod.add_item_barcode, conn, args(env, item_id=env["item2"]))
    assert duplicate["status"] == "error"
    assert snapshot(conn) == before


@pytest.mark.parametrize("changes", [
    {"barcode": ""}, {"barcode": " A"}, {"barcode": "A\nB"},
    {"barcode": "x" * 129}, {"company_id": None}, {"company_id": "missing"},
    {"item_id": "missing"},
])
def test_bad_mapping_writes_nothing(mod, conn, env, changes):
    before = snapshot(conn)
    result = call_action(mod.add_item_barcode, conn, args(env, **changes))
    assert result["status"] == "error"
    assert snapshot(conn) == before


@pytest.mark.parametrize("operation", ["receive", "issue", "transfer", "count"])
def test_normal_drafts_and_submission(mod, conn, env, db_path, operation):
    register(mod, conn, env)
    if operation == "receive":
        line = receive(env)
    elif operation == "issue":
        seed_account(conn, env["company_id"], "Cost of Goods Sold", "expense",
                     "cost_of_goods_sold", "5300")
        line = {"barcode": "WIDGET-A", "qty": "2.50",
                "from_warehouse_id": env["warehouse"]}
    elif operation == "transfer":
        line = {"barcode": "WIDGET-A", "qty": "2.50",
                "from_warehouse_id": env["warehouse"],
                "to_warehouse_id": env["warehouse2"]}
    else:
        line = {"barcode": "WIDGET-A", "qty": "98.00", "valuation_rate": "50.00",
                "warehouse_id": env["warehouse"]}
    before = snapshot(conn)
    fn = mod.add_scanned_stock_count if operation == "count" else mod.add_scanned_stock_entry
    result = call_action(fn, conn, args(env, entry_type=operation, scans=json.dumps([line])))
    assert result["status"] == "ok", result
    draft = snapshot(conn)
    assert draft["gl_entry"] == before["gl_entry"]
    assert draft["stock_ledger_entry"] == before["stock_ledger_entry"]
    if operation == "count":
        assert result["difference_amount"] == "-100.00"
        submission = call_action(mod.submit_stock_reconciliation, conn, ns(
            stock_reconciliation_id=result["stock_reconciliation_id"]))
    else:
        assert result["total_incoming_value"] == ("50.00" if operation == "receive"
                                                  else "125.00" if operation == "transfer" else "0.00")
        submission = call_action(mod.submit_stock_entry, conn, ns(
            stock_entry_id=result["stock_entry_id"]))
    assert submission["status"] == "ok", submission
    proof = check_gl_invariants(db_path)
    assert proof["result"] == "pass", proof
    assert proof["unexamined"] == []
    assert proof["violations"] == []


@pytest.mark.parametrize("change", [
    {"barcode": "unknown"}, {"qty": "0"}, {"qty": "-1"}, {"qty": "NaN"},
    {"qty": "Infinity"}, {"qty": "0.001"}, {"qty": 2}, {"qty": "1000000001"},
    {"rate": "0"}, {"rate": "NaN"}, {"rate": "0.001"},
    {"to_warehouse_id": "missing"}, {"item_id": "override"},
])
def test_invalid_second_scan_refuses_all_writes(mod, conn, env, change):
    register(mod, conn, env)
    good, bad = receive(env), receive(env)
    bad.update(change)
    before = snapshot(conn)
    result = call_action(mod.add_scanned_stock_entry, conn, args(
        env, scans=json.dumps([good, bad])))
    assert result["status"] == "error", result
    assert snapshot(conn) == before


@pytest.mark.parametrize("operation", ["receive", "issue", "transfer", "count"])
def test_foreign_warehouse_refusal(mod, conn, env, operation):
    register(mod, conn, env)
    other = seed_company(conn, "Other", "OT")
    warehouse = seed_warehouse(conn, other, "Other warehouse")
    line = {"barcode": "WIDGET-A", "qty": "2.00"}
    if operation == "count":
        line.update(warehouse_id=warehouse, valuation_rate="10.00")
    else:
        if operation in ("issue", "transfer"):
            line["from_warehouse_id"] = warehouse
        if operation in ("receive", "transfer"):
            line["to_warehouse_id"] = env["warehouse"] if operation == "transfer" else warehouse
        if operation == "receive":
            line["rate"] = "10.00"
    before = snapshot(conn)
    fn = mod.add_scanned_stock_count if operation == "count" else mod.add_scanned_stock_entry
    result = call_action(fn, conn, args(env, entry_type=operation, scans=json.dumps([line])))
    assert result["status"] == "error"
    assert snapshot(conn) == before


@pytest.mark.parametrize("changes", [
    {"posting_date": "2026-02-30"}, {"posting_date": None}, {"items": "[]"},
    {"scans": "[]"}, {"scans": "{}"}, {"scans": "invalid"},
    {"dimensions": '{"site": "A"}'}, {"entry_type": "manufacture"},
])
def test_invalid_scan_request_writes_nothing(mod, conn, env, changes):
    register(mod, conn, env)
    data = dict(scans=json.dumps([receive(env)]))
    data.update(changes)
    before = snapshot(conn)
    result = call_action(mod.add_scanned_stock_entry, conn, args(env, **data))
    assert result["status"] == "error"
    assert snapshot(conn) == before


def test_duplicate_and_same_warehouse_transfer_refusal(mod, conn, env):
    register(mod, conn, env)
    before = snapshot(conn)
    for lines, kind in (([receive(env), receive(env)], "receive"), ([{
        "barcode": "WIDGET-A", "qty": "1.00", "from_warehouse_id": env["warehouse"],
        "to_warehouse_id": env["warehouse"]}], "transfer")):
        result = call_action(mod.add_scanned_stock_entry, conn, args(
            env, scans=json.dumps(lines), entry_type=kind))
        assert result["status"] == "error"
        assert snapshot(conn) == before


@pytest.mark.parametrize("column,value", [("status", "disabled"), ("item_type", "service"),
                                         ("has_batch", 1), ("has_serial", 1)])
def test_unusable_stock_refused_at_scan_time(mod, conn, env, column, value):
    register(mod, conn, env)
    item = Table("item")
    conn.execute(Q.update(item).set(column, P()).where(item.id == P()).get_sql(),
                 (value, env["item1"]))
    conn.commit()
    before = snapshot(conn)
    result = call_action(mod.add_scanned_stock_entry, conn, args(
        env, scans=json.dumps([receive(env)])))
    assert result["status"] == "error"
    assert snapshot(conn) == before


def test_barcode_migration_idempotent_and_preserves_legacy_data(conn, env, db_path):
    path = Path(__file__).parents[2] / "erpclaw-setup" / "migrations" / "059_item_barcode.py"
    spec = importlib.util.spec_from_file_location("barcode_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    item = Table("item")
    conn.execute(Q.update(item).set(item.barcode, P()).where(item.id == P()).get_sql(),
                 ("LEGACY", env["item1"]))
    conn.commit()
    before = snapshot(conn)
    assert migration.MIGRATION_DATA_CLASS == "none"
    assert migration.run_migration(db_path) == {"provisioned": True}
    assert migration.run_migration(db_path) == {"provisioned": True}
    assert snapshot(conn) == before
    assert seam.column_names("item_barcode", db_path) == ["id", "company_id", "item_id", "barcode"]
    assert conn.execute(Q.from_(item).select(item.barcode).where(item.id == P()).get_sql(),
                        (env["item1"],)).fetchone()[0] == "LEGACY"


def test_upgrade_provisions_absent_store_without_guessing_legacy_ownership(tmp_path):
    from erpclaw_lib.seam import MetaData, Column, Text, Table as SchemaTable
    path = str(tmp_path / "older.sqlite")
    older = MetaData()
    SchemaTable("company", older, Column("id", Text, primary_key=True))
    SchemaTable("item", older, Column("id", Text, primary_key=True), Column("barcode", Text))
    seam.provision(older, path)
    with get_connection(path) as old:
        item = Table("item")
        old.execute(Q.into(item).columns("id", "barcode").insert(P(), P()).get_sql(),
                    ("legacy-item", "OLD-CODE"))
        old.commit()
    assert not seam.table_exists("item_barcode", path)
    file = Path(__file__).parents[2] / "erpclaw-setup" / "migrations" / "059_item_barcode.py"
    spec = importlib.util.spec_from_file_location("barcode_upgrade", file)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.run_migration(path)
    migration.run_migration(path)
    assert seam.table_exists("item_barcode", path)
    with get_connection(path) as upgraded:
        mapping = Table("item_barcode")
        assert upgraded.execute(Q.from_(mapping).select(mapping.id).get_sql()).fetchall() == []
        assert upgraded.execute(Q.from_(item).select(item.barcode).get_sql()).fetchone()[0] == "OLD-CODE"


@pytest.mark.parametrize("operation", ["receive", "issue"])
def test_scan_does_not_bypass_open_orders(mod, conn, env, operation):
    from test_open_order_guard import (
        seed_supplier, seed_customer, seed_purchase_order, seed_sales_order,
    )
    register(mod, conn, env)
    if operation == "receive":
        supplier = seed_supplier(conn, env["company_id"])
        seed_purchase_order(conn, env["company_id"], supplier, [(env["item1"], "10", "0")])
        line = receive(env)
    else:
        customer = seed_customer(conn, env["company_id"])
        seed_sales_order(conn, env["company_id"], customer, [(env["item1"], "10", "0")])
        line = {"barcode": "WIDGET-A", "qty": "1.00", "from_warehouse_id": env["warehouse"]}
    before = snapshot(conn)
    result = call_action(mod.add_scanned_stock_entry, conn, args(
        env, entry_type=operation, scans=json.dumps([line])))
    assert result["status"] == "error"
    assert "order" in result["message"]
    assert snapshot(conn) == before


def test_foreign_mapping_and_group_warehouse_are_not_used(mod, conn, env):
    other = seed_company(conn, "Other", "OT")
    register(mod, conn, env, company_id=other)
    before = snapshot(conn)
    result = call_action(mod.add_scanned_stock_entry, conn, args(env, scans=json.dumps([receive(env)])))
    assert result["status"] == "error"
    assert snapshot(conn) == before
    register(mod, conn, env)
    warehouse = Table("warehouse")
    conn.execute(Q.update(warehouse).set(warehouse.is_group, 1).where(warehouse.id == P()).get_sql(),
                 (env["warehouse"],))
    conn.commit()
    before = snapshot(conn)
    result = call_action(mod.add_scanned_stock_entry, conn, args(env, scans=json.dumps([receive(env)])))
    assert result["status"] == "error"
    assert snapshot(conn) == before


def test_root_router_creates_scanned_draft(conn, env, db_path, tmp_path):
    root = Path(__file__).parents[2]
    home = tmp_path / "install"
    home.mkdir()
    (home / "lib").symlink_to(root / "erpclaw-setup" / "lib", target_is_directory=True)
    conn.commit()
    conn.close()
    shutil.copy2(db_path, home / "data.sqlite")
    runtime = os.environ.copy()
    runtime["ERPCLAW_HOME"] = str(home)
    runtime.pop("ERPCLAW_DB_PATH", None)
    router = root / "db_query.py"
    for flags in (["--action", "add-item-barcode", "--company-id", env["company_id"],
                   "--item-id", env["item1"], "--barcode", "WIDGET-A"],
                  ["--action", "add-scanned-stock-entry", "--company-id", env["company_id"],
                   "--entry-type", "receive", "--posting-date", "2026-06-15",
                   "--scans", json.dumps([receive(env)], indent=700)]):
        run = subprocess.run([sys.executable, str(router)] + flags, env=runtime,
                             capture_output=True, text=True, timeout=30)
        assert run.returncode == 0, run.stderr + run.stdout
        assert json.loads(run.stdout)["status"] == "ok"
