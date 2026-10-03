"""Part A — migration 039: receipt lines and bill lines carry discount_amount.

The purchase-order line records a discount, and the receipt line and the bill
line it turns into had nowhere to hold their share of it. Fresh installs
declare `discount_amount` (money text, zero by default) last on both line
tables; this migration adds the same column, last, on installs that already
exist.

Every pin runs the REAL migration module against a real database initialized
by `init_schema` and then rewound to its genuine pre-039 shape (the column
dropped from both tables), with rows planted exactly as the pre-039 code
wrote them. The pins are weighted toward what the migration must NOT do:

  * it must not rewrite a single stored value: every pre-039 column reads
    back identical after the run;
  * it must not write anything at all in `report_only`;
  * it must reach the same end state when run twice, and after a crash
    between the two ADDs (each ADD is guarded on its own column, so a rerun
    only adds what is still missing);
  * a migrated database must describe exactly like a fresh one, order
    included.
"""
import importlib.util
import os
import sys
import uuid

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "039_purchase_line_discount_columns.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mig():
    return _load("migration_039", _MIGRATION)


from setup_helpers import init_all_tables, seed_company, seed_supplier, read_one  # noqa: E402  (binds erpclaw_lib to this tree)
from erpclaw_lib import seam  # noqa: E402  (after the lib binding in setup_helpers)


# ── fixtures: a database rewound to its genuine pre-039 shape ────────────────

_REWIND_RECEIPT = "ALTER TABLE purchase_receipt_item DROP COLUMN discount_amount"
_REWIND_INVOICE = "ALTER TABLE purchase_invoice_item DROP COLUMN discount_amount"


def _rewind(conn, db_path):
    """Drop the 039 column from both line tables, so they are shaped as they
    shipped before. A fresh install already has 039 applied, so every pin
    here has to put the database back before the migration can do anything."""
    conn.execute(_REWIND_RECEIPT)
    conn.execute(_REWIND_INVOICE)
    conn.commit()
    assert "discount_amount" not in seam.column_names(
        "purchase_receipt_item", db_path)
    assert "discount_amount" not in seam.column_names(
        "purchase_invoice_item", db_path)


def _headers(conn, company_id, supplier_id):
    rid = str(uuid.uuid4())
    iid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO purchase_receipt (id, supplier_id, posting_date, "
        " company_id) VALUES (?, ?, '2026-01-15', ?)",
        (rid, supplier_id, company_id))
    conn.execute(
        "INSERT INTO purchase_invoice (id, supplier_id, posting_date, "
        " company_id) VALUES (?, ?, '2026-01-15', ?)",
        (iid, supplier_id, company_id))
    conn.commit()
    return rid, iid


def _lines(conn, receipt_id, invoice_id):
    """Write one receipt line and one bill line exactly as the pre-039 code
    wrote them: the same columns, never naming the new one."""
    rl = str(uuid.uuid4())
    bl = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO purchase_receipt_item (id, purchase_receipt_id, item_id, "
        " quantity, rate, amount) VALUES (?, ?, ?, '3', '10.00', '30.00')",
        (rl, receipt_id, str(uuid.uuid4())))
    conn.execute(
        "INSERT INTO purchase_invoice_item (id, purchase_invoice_id, item_id, "
        " quantity, rate, amount) VALUES (?, ?, ?, '3', '10.00', '30.00')",
        (bl, invoice_id, str(uuid.uuid4())))
    conn.commit()
    return rl, bl


def _seeded(conn, db_path):
    company_id = seed_company(conn)
    supplier_id = seed_supplier(conn, company_id)
    rid, iid = _headers(conn, company_id, supplier_id)
    return _lines(conn, rid, iid)


def test_fresh_install_declares_both_columns(db_path, conn):
    for table in ("purchase_receipt_item", "purchase_invoice_item"):
        assert seam.column_names(table, db_path)[-1] == "discount_amount"
    company_id = seed_company(conn)
    supplier_id = seed_supplier(conn, company_id)
    rid, iid = _headers(conn, company_id, supplier_id)
    rl, bl = _lines(conn, rid, iid)
    assert read_one(conn, "purchase_receipt_item",
                    ["discount_amount"], rl)["discount_amount"] == "0"
    assert read_one(conn, "purchase_invoice_item",
                    ["discount_amount"], bl)["discount_amount"] == "0"


def test_migration_adds_columns_and_keeps_every_existing_value(db_path, conn):
    mig = _mig()
    _rewind(conn, db_path)
    rl, bl = _seeded(conn, db_path)
    before_receipt = seam.column_names("purchase_receipt_item", db_path)
    before_invoice = seam.column_names("purchase_invoice_item", db_path)
    row_receipt = read_one(conn, "purchase_receipt_item", before_receipt, rl)
    row_invoice = read_one(conn, "purchase_invoice_item", before_invoice, bl)
    result = mig.run_migration(db_path)
    assert result["added"] == list(mig.TABLES)
    assert result["present"] == []
    assert result["absent"] == []
    assert result["report_only"] is False
    after_receipt = read_one(conn, "purchase_receipt_item",
                             before_receipt + ["discount_amount"], rl)
    after_invoice = read_one(conn, "purchase_invoice_item",
                             before_invoice + ["discount_amount"], bl)
    for column in before_receipt:
        assert after_receipt[column] == row_receipt[column]
    for column in before_invoice:
        assert after_invoice[column] == row_invoice[column]
    assert after_receipt["discount_amount"] == "0"
    assert after_invoice["discount_amount"] == "0"


def test_second_run_is_a_no_op(db_path, conn):
    mig = _mig()
    _rewind(conn, db_path)
    rl, bl = _seeded(conn, db_path)
    first = mig.run_migration(db_path)
    assert first["added"] == list(mig.TABLES)
    columns_receipt = seam.column_names("purchase_receipt_item", db_path)
    columns_invoice = seam.column_names("purchase_invoice_item", db_path)
    row_receipt = read_one(conn, "purchase_receipt_item", columns_receipt, rl)
    row_invoice = read_one(conn, "purchase_invoice_item", columns_invoice, bl)
    second = mig.run_migration(db_path)
    assert second["added"] == []
    assert second["present"] == list(mig.TABLES)
    assert second["absent"] == []
    assert seam.column_names("purchase_receipt_item", db_path) == columns_receipt
    assert seam.column_names("purchase_invoice_item", db_path) == columns_invoice
    assert read_one(conn, "purchase_receipt_item",
                    columns_receipt, rl) == row_receipt
    assert read_one(conn, "purchase_invoice_item",
                    columns_invoice, bl) == row_invoice


def test_report_only_writes_nothing(db_path, conn):
    mig = _mig()
    _rewind(conn, db_path)
    _seeded(conn, db_path)
    before_receipt = seam.column_names("purchase_receipt_item", db_path)
    before_invoice = seam.column_names("purchase_invoice_item", db_path)
    result = mig.run_migration(db_path, report_only=True)
    assert result["added"] == []
    assert result["present"] == []
    assert result["absent"] == []
    assert result["report_only"] is True
    assert "discount_amount" not in seam.column_names(
        "purchase_receipt_item", db_path)
    assert "discount_amount" not in seam.column_names(
        "purchase_invoice_item", db_path)
    assert seam.column_names("purchase_receipt_item", db_path) == before_receipt
    assert seam.column_names("purchase_invoice_item", db_path) == before_invoice


def test_migrated_matches_fresh(db_path, conn, tmp_path):
    mig = _mig()
    _rewind(conn, db_path)
    mig.run_migration(db_path)
    fresh = str(tmp_path / "fresh.sqlite")
    init_all_tables(fresh)
    for table in ("purchase_receipt_item", "purchase_invoice_item"):
        assert (seam.describe_table(table, db_path)
                == seam.describe_table(table, fresh))
        discount = [c for c in seam.describe_table(table, db_path)["columns"]
                    if c["name"] == "discount_amount"]
        assert len(discount) == 1
        assert discount[0]["type"] == "TEXT"
        assert discount[0]["nullable"] is False


def test_absent_table_is_not_an_error(db_path, conn):
    mig = _mig()
    _rewind(conn, db_path)
    conn.execute("DROP TABLE purchase_receipt_item")
    conn.execute("DROP TABLE purchase_invoice_item")
    conn.commit()
    result = mig.run_migration(db_path)
    assert result["absent"] == list(mig.TABLES)
    assert result["added"] == []
    assert result["present"] == []


def test_declares_no_data_change():
    assert _mig().MIGRATION_DATA_CLASS == "none"


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; the PG lane "
           "runs on the box leg, plan §8.3)")
def test_pg_lane_adds_both_columns_and_is_idempotent(monkeypatch):
    """Runs against an expendable database only — never point it at shared data."""
    from urllib.parse import unquote, urlparse

    from erpclaw_lib.db import get_connection

    mig = _mig()
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", _PG_URL)
    expected_db = unquote(urlparse(_PG_URL).path.lstrip("/"))
    guard = get_connection(_PG_URL)
    try:
        current_db = guard.execute("SELECT current_database()").fetchone()[0]
        server_addr = guard.execute("SELECT inet_server_addr()").fetchone()[0]
        listen = guard.execute(
            "SELECT current_setting('listen_addresses')").fetchone()[0]
        version_num = guard.execute(
            "SELECT current_setting('server_version_num')").fetchone()[0]
        version = guard.execute("SELECT version()").fetchone()[0]
    finally:
        guard.close()
    print("version(): %s" % version)
    print("current_database(): %s" % current_db)
    if current_db != expected_db:
        pytest.fail("refusing: connected database is not the one in the URL")
    if server_addr is not None:
        pytest.fail("refusing: server is reachable over the network")
    if listen != "":
        pytest.fail("refusing: server listens on an address")
    if not str(version_num).startswith("16"):
        pytest.fail("refusing: server is not PostgreSQL 16")
    init_all_tables(_PG_URL)
    rewind = get_connection(_PG_URL)
    try:
        rewind.execute("ALTER TABLE purchase_receipt_item "
                       "DROP COLUMN IF EXISTS discount_amount")
        rewind.execute("ALTER TABLE purchase_invoice_item "
                       "DROP COLUMN IF EXISTS discount_amount")
        rewind.commit()
    finally:
        rewind.close()
    first = mig.run_migration(_PG_URL)
    assert first["added"] == list(mig.TABLES)
    assert "discount_amount" in seam.column_names(
        "purchase_receipt_item", _PG_URL)
    assert "discount_amount" in seam.column_names(
        "purchase_invoice_item", _PG_URL)
    second = mig.run_migration(_PG_URL)
    assert second["added"] == []
    assert second["present"] == list(mig.TABLES)
