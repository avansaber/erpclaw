"""Part 1 — migration 046: business documents carry dimensions_json.

Every document table gains `dimensions_json` (text, `'{}'` by default) last,
so a document carries the accounting dimensions its ledger rows post. Fresh
installs declare the column; this migration adds the same column, last, on
installs that already exist.

Every pin runs the REAL migration module against a real database initialized
by `init_schema` and then rewound to its genuine pre-046 shape (the column
dropped from all nineteen tables), with rows planted exactly as the pre-046
code wrote them. The pins are weighted toward what the migration must NOT do:

  * it must not rewrite a single stored value: every pre-046 column reads
    back identical after the run;
  * it must not write anything at all in `report_only`;
  * it must reach the same end state when run twice, and after a crash
    between the ADDs (each ADD is guarded on its own column, so a rerun
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
                          "046_document_dimension_columns.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mig():
    return _load("migration_046", _MIGRATION)


from setup_helpers import (  # noqa: E402  (binds erpclaw_lib to this tree)
    init_all_tables, seed_account, seed_company, seed_customer, read_one)
from erpclaw_lib import seam  # noqa: E402  (after the lib binding in setup_helpers)


# ── fixtures: a database rewound to its genuine pre-046 shape ────────────────

# The nineteen document tables, spelled out here — never read off the
# migration module — so a table dropped from the migration's TABLES still
# fails the pins below instead of silently shrinking them.
_EXPECTED_TABLES = ("journal_entry", "journal_entry_line",
                    "recurring_journal_template", "payment_entry",
                    "quotation", "sales_order", "delivery_note",
                    "sales_invoice", "recurring_invoice_template",
                    "purchase_order", "purchase_receipt", "purchase_invoice",
                    "landed_cost_voucher", "recurring_bill_template",
                    "stock_entry", "stock_reconciliation", "stock_revaluation",
                    "expense_claim", "payroll_run")

_REWINDS = tuple(
    "ALTER TABLE %s DROP COLUMN dimensions_json" % table
    for table in ("journal_entry", "journal_entry_line",
                  "recurring_journal_template", "payment_entry", "quotation",
                  "sales_order", "delivery_note", "sales_invoice",
                  "recurring_invoice_template", "purchase_order",
                  "purchase_receipt", "purchase_invoice",
                  "landed_cost_voucher", "recurring_bill_template",
                  "stock_entry", "stock_reconciliation", "stock_revaluation",
                  "expense_claim", "payroll_run"))


def _rewind(conn, db_path):
    """Drop the 046 column from all nineteen tables, so they are shaped as
    they shipped before. A fresh install already has 046 applied, so every
    pin here has to put the database back before the migration can do
    anything."""
    for statement in _REWINDS:
        conn.execute(statement)
    conn.commit()
    for table in _EXPECTED_TABLES:
        assert "dimensions_json" not in seam.column_names(table, db_path)


def _seeded(conn, db_path):
    """Plant one row in three document tables exactly as the pre-046 code
    wrote them: the same columns, never naming the new one."""
    company_id = seed_company(conn)
    customer_id = seed_customer(conn, company_id)
    from_acct = seed_account(conn, company_id, name="Cash",
                             root_type="asset", account_type="cash")
    to_acct = seed_account(conn, company_id, name="Bank",
                           root_type="asset", account_type="bank")
    je = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO journal_entry (id, posting_date, company_id) "
        "VALUES (?, '2026-02-10', ?)",
        (je, company_id))
    si = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO sales_invoice (id, customer_id, posting_date, "
        " company_id) VALUES (?, ?, '2026-02-10', ?)",
        (si, customer_id, company_id))
    pe = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO payment_entry (id, payment_type, posting_date, "
        " paid_from_account, paid_to_account, company_id) "
        "VALUES (?, 'receive', '2026-02-10', ?, ?, ?)",
        (pe, from_acct, to_acct, company_id))
    conn.commit()
    return {"journal_entry": je, "sales_invoice": si, "payment_entry": pe}


def test_migration_covers_all_document_tables():
    assert tuple(_mig().TABLES) == _EXPECTED_TABLES


def test_fresh_install_declares_the_column_last(db_path, conn):
    mig = _mig()
    for table in _EXPECTED_TABLES:
        assert seam.column_names(table, db_path)[-1] == "dimensions_json"
    ids = _seeded(conn, db_path)
    for table, row_id in ids.items():
        assert read_one(conn, table, ["dimensions_json"], row_id)[
            "dimensions_json"] == "{}"


def test_migration_adds_columns_and_keeps_every_existing_value(db_path, conn):
    mig = _mig()
    _rewind(conn, db_path)
    ids = _seeded(conn, db_path)
    before = {table: seam.column_names(table, db_path)
              for table in _EXPECTED_TABLES}
    rows = {table: read_one(conn, table, before[table], ids[table])
            for table in ids}
    result = mig.run_migration(db_path)
    assert result["added"] == list(mig.TABLES)
    assert result["present"] == []
    assert result["absent"] == []
    assert result["report_only"] is False
    for table in _EXPECTED_TABLES:
        assert seam.column_names(table, db_path)[-1] == "dimensions_json"
    for table, row_id in ids.items():
        after = read_one(conn, table, before[table] + ["dimensions_json"],
                         row_id)
        for column in before[table]:
            assert after[column] == rows[table][column]
        assert after["dimensions_json"] == "{}"


def test_second_run_is_a_no_op(db_path, conn):
    mig = _mig()
    _rewind(conn, db_path)
    ids = _seeded(conn, db_path)
    first = mig.run_migration(db_path)
    assert first["added"] == list(mig.TABLES)
    columns = {table: seam.column_names(table, db_path)
               for table in _EXPECTED_TABLES}
    rows = {table: read_one(conn, table, columns[table], ids[table])
            for table in ids}
    second = mig.run_migration(db_path)
    assert second["added"] == []
    assert second["present"] == list(mig.TABLES)
    assert second["absent"] == []
    for table in _EXPECTED_TABLES:
        assert seam.column_names(table, db_path) == columns[table]
    for table, row_id in ids.items():
        assert read_one(conn, table, columns[table], row_id) == rows[table]


def test_report_only_writes_nothing(db_path, conn, capsys):
    mig = _mig()
    _rewind(conn, db_path)
    _seeded(conn, db_path)
    before = {table: seam.column_names(table, db_path)
              for table in _EXPECTED_TABLES}
    result = mig.run_migration(db_path, report_only=True)
    assert result["added"] == []
    assert result["present"] == []
    assert result["absent"] == []
    assert result["report_only"] is True
    for table in _EXPECTED_TABLES:
        assert "dimensions_json" not in seam.column_names(table, db_path)
        assert seam.column_names(table, db_path) == before[table]
    out = capsys.readouterr().out
    assert sum("would be added" in line for line in out.splitlines()) == 19


def test_crash_partway_recovers_on_rerun(db_path, conn, tmp_path,
                                         monkeypatch):
    mig = _mig()
    _rewind(conn, db_path)
    _seeded(conn, db_path)
    failing = ("ALTER TABLE m686b_no_such_table ADD COLUMN dimensions_json "
               "TEXT NOT NULL DEFAULT '{}'")
    monkeypatch.setitem(mig._STATEMENTS, mig.TABLES[9], failing)
    with pytest.raises(Exception):
        mig.run_migration(db_path)
    monkeypatch.undo()
    rerun = mig.run_migration(db_path)
    assert set(rerun["added"]) | set(rerun["present"]) == set(mig.TABLES)
    fresh = str(tmp_path / "fresh.sqlite")
    init_all_tables(fresh)
    for table in _EXPECTED_TABLES:
        assert (seam.describe_table(table, db_path)
                == seam.describe_table(table, fresh))


def test_migrated_matches_fresh(db_path, conn, tmp_path):
    mig = _mig()
    _rewind(conn, db_path)
    mig.run_migration(db_path)
    fresh = str(tmp_path / "fresh.sqlite")
    init_all_tables(fresh)
    for table in _EXPECTED_TABLES:
        assert (seam.describe_table(table, db_path)
                == seam.describe_table(table, fresh))
        dimensions = [c for c in seam.describe_table(table, db_path)["columns"]
                      if c["name"] == "dimensions_json"]
        assert len(dimensions) == 1
        assert dimensions[0]["type"] == "TEXT"
        assert dimensions[0]["nullable"] is False


def test_absent_table_is_not_an_error(db_path, conn):
    mig = _mig()
    _rewind(conn, db_path)
    conn.execute("DROP TABLE payroll_run")
    conn.commit()
    result = mig.run_migration(db_path)
    assert result["absent"] == ["payroll_run"]
    assert set(result["added"]) == set(mig.TABLES) - {"payroll_run"}
    assert result["present"] == []


def test_declares_no_data_change():
    assert _mig().MIGRATION_DATA_CLASS == "none"


_PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")


@pytest.mark.skipif(
    not _PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required; the PG lane "
           "runs on the box leg, plan §8.3)")
def test_pg_lane_adds_all_columns_and_is_idempotent(monkeypatch):
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
        for statement in _REWINDS:
            rewind.execute(statement)
        rewind.commit()
    finally:
        rewind.close()
    first = mig.run_migration(_PG_URL)
    assert first["added"] == list(mig.TABLES)
    for table in _EXPECTED_TABLES:
        assert "dimensions_json" in seam.column_names(table, _PG_URL)
    second = mig.run_migration(_PG_URL)
    assert second["added"] == []
    assert second["present"] == list(mig.TABLES)
