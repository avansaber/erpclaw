"""Stock balance and batches-by-warehouse on both numeric backends.

The stock-balance and batches-by-warehouse reports compare exact decimal
sums: a zero net balance must read as zero, never as float dust, and the
same statements must run on PostgreSQL, where TEXT arithmetic (`+ 0`) and
under-grouped SELECTs are refused.

Seed: one warehouse; item X receives 5 and issues 5 (net 0); item Y
receives 3.5 at 10.00; a batch of Y holds the 3.5. The balance report
returns only Y (`qty "3.50"`, `stock_value "35.00"`,
`total_stock_value "35.00"`, `row_count 1`); `list-batches
--warehouse-id` returns only Y's batch with total count 1.
"""
import os
import sys
import uuid
from decimal import Decimal
from urllib.parse import urlparse

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import pytest

from inventory_helpers import call_action, ns, is_ok, load_db_query

SETUP_DIR = os.path.join(os.path.dirname(os.path.dirname(_TESTS_DIR)),
                         "erpclaw-setup")
_IN_TREE_LIB = os.path.join(SETUP_DIR, "lib")
if _IN_TREE_LIB not in sys.path:
    import importlib as _il
    if _il.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, _IN_TREE_LIB)

import importlib.util as _ilu

from erpclaw_lib.db import get_connection
from erpclaw_lib.query import P, insert_row
from erpclaw_lib import seam as _seam

mod = load_db_query()


def _insert(conn, table, **data):
    sql, cols = insert_row(table, {k: P() for k in data})
    conn.execute(sql, [data[c] for c in cols])


def _seed_sle(conn, item_id, warehouse_id, qty, rate, batch_id=None,
              posting_date="2026-01-15"):
    sid = str(uuid.uuid4())
    value = str(Decimal(qty) * Decimal(rate))
    _insert(conn, "stock_ledger_entry", id=sid, posting_date=posting_date,
            item_id=item_id, warehouse_id=warehouse_id, actual_qty=qty,
            qty_after_transaction=qty, valuation_rate=rate,
            stock_value=value, stock_value_difference=value,
            voucher_type="stock_entry", voucher_id="INIT-%s" % sid[:8],
            batch_id=batch_id, is_cancelled=0)
    return sid


def seed_numeric_data(conn, company_id):
    """Seed the X-net-zero / Y-holds-3.5 ledger described above."""
    wid = str(uuid.uuid4())
    _insert(conn, "warehouse", id=wid, name="Numeric WH",
            company_id=company_id)
    xid = str(uuid.uuid4())
    _insert(conn, "item", id=xid, item_code="NUM-X-%s" % xid[:6],
            item_name="Numeric X")
    yid = str(uuid.uuid4())
    _insert(conn, "item", id=yid, item_code="NUM-Y-%s" % yid[:6],
            item_name="Numeric Y")
    _seed_sle(conn, xid, wid, "5", "10.00")
    _seed_sle(conn, xid, wid, "-5", "10.00")
    batch_id = str(uuid.uuid4())
    _insert(conn, "batch", id=batch_id,
            batch_name="NUM-Y-BATCH-%s" % batch_id[:6], item_id=yid)
    _seed_sle(conn, yid, wid, "3.5", "10.00", batch_id=batch_id)
    conn.commit()
    return {"warehouse_id": wid, "item_x": xid, "item_y": yid,
            "batch_id": batch_id}


def check_stock_balance(result, item_y):
    assert is_ok(result), result
    assert result["row_count"] == 1
    assert result["total_stock_value"] == "35.00"
    (row,) = result["report"]
    assert row["item_id"] == item_y
    assert row["qty"] == "3.50"
    assert row["stock_value"] == "35.00"


def check_batches(result, batch_id):
    assert is_ok(result), result
    assert result["total_count"] == 1
    (row,) = result["batches"]
    assert row["id"] == batch_id


@pytest.fixture
def numeric_env(conn):
    from inventory_helpers import seed_company
    cid = seed_company(conn)
    return {"company_id": cid, **seed_numeric_data(conn, cid)}


class TestStockBalanceNumericBackends:
    def test_balance_report_hides_zero_net_item(self, conn, numeric_env):
        check_stock_balance(call_action(
            mod.stock_balance_report, conn,
            ns(company_id=numeric_env["company_id"],
               warehouse_id=None)),
            numeric_env["item_y"])

    def test_batches_by_warehouse_hides_empty_batch(self, conn, numeric_env):
        check_batches(call_action(
            mod.list_batches, conn,
            ns(item_id=None,
               warehouse_id=numeric_env["warehouse_id"],
               limit=None, offset=None)),
            numeric_env["batch_id"])


@pytest.fixture(autouse=True)
def _dispose_seam_engines():
    yield
    _seam.dispose_engines()


@pytest.fixture
def pg_pair(monkeypatch):
    """Live PostgreSQL connection plus the numeric env, reset per test."""
    pg_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not pg_url:
        pytest.skip("ERPCLAW_PG_TEST_URL not set (live PostgreSQL required)")
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", pg_url)
    if "ERPCLAW_DB_PATH" in os.environ:
        monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    expected_db = urlparse(pg_url).path.strip("/")
    if not expected_db:
        raise RuntimeError("refusing to reset: ERPCLAW_PG_TEST_URL names no database")
    setup_conn = get_connection()
    try:
        resolved_db = setup_conn.execute("SELECT current_database()").fetchone()[0]
        if resolved_db != expected_db:
            raise RuntimeError(
                "refusing to reset: ERPCLAW_PG_TEST_URL names database %r "
                "but the connection resolved to %r" % (expected_db, resolved_db))
        setup_conn.execute("DROP SCHEMA public CASCADE")
        setup_conn.execute("CREATE SCHEMA public")
        setup_conn.commit()
    finally:
        setup_conn.close()
    init_schema_path = os.path.join(SETUP_DIR, "init_schema.py")
    spec = _ilu.spec_from_file_location("init_schema_pg_leg", init_schema_path)
    schema_mod = _ilu.module_from_spec(spec)
    spec.loader.exec_module(schema_mod)
    schema_mod.init_db(None)
    conn = get_connection()
    cid = str(uuid.uuid4())
    _insert(conn, "company", id=cid, name="Numeric Co %s" % cid[:6],
            abbr="NM%s" % cid[:4], default_currency="USD",
            country="United States", fiscal_year_start_month=1)
    conn.commit()
    env = {"company_id": cid, **seed_numeric_data(conn, cid)}
    try:
        yield conn, env
    finally:
        conn.close()


class TestStockBalanceNumericBackendsPG:
    def test_balance_report_hides_zero_net_item(self, pg_pair):
        conn, env = pg_pair
        check_stock_balance(call_action(
            mod.stock_balance_report, conn,
            ns(company_id=env["company_id"], warehouse_id=None)),
            env["item_y"])

    def test_batches_by_warehouse_hides_empty_batch(self, pg_pair):
        conn, env = pg_pair
        check_batches(call_action(
            mod.list_batches, conn,
            ns(item_id=None, warehouse_id=env["warehouse_id"],
               limit=None, offset=None)),
            env["batch_id"])
