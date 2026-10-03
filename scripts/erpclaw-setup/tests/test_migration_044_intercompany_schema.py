"""Migration 044: intercompany linkage columns and the account-map table (M677).

The five intercompany actions read and write
``sales_invoice.is_intercompany`` / ``.intercompany_reference_id``, the same
two columns on ``purchase_invoice``, and the ``intercompany_account_map``
table. ``init_schema`` now declares all six objects on a fresh install; this
migration carries existing installs to that same shape.

Pins:
  1. a fresh install already has the schema, and the migration is a no-op
     that changes nothing observable;
  2. an install rewound to its genuine pre-044 shape (columns dropped, table
     dropped, seeded rows kept) migrates to a shape byte-identical to fresh
     — column order included — with seeded rows intact, and a second run is
     a no-op;
  3. the same body on live PostgreSQL.

Reads go through PyPika and the seam; DDL text lives only in the rewind
constants below.
"""
import importlib.util
import os
import sys
import uuid
from urllib.parse import urlparse

import pytest

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_SETUP_DIR = os.path.dirname(_TESTS_DIR)
_MIGRATION = os.path.join(_SETUP_DIR, "migrations",
                          "044_intercompany_schema.py")
_MIGRATION_046 = os.path.join(_SETUP_DIR, "migrations",
                              "046_document_dimension_columns.py")
_INIT_SCHEMA = os.path.join(_SETUP_DIR, "init_schema.py")

if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load("migration_044", _MIGRATION)
mig046 = _load("migration_046", _MIGRATION_046)

from erpclaw_lib import seam  # noqa: E402  (setup_helpers binds the tree lib first)
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Q, P, Table  # noqa: E402

_TABLES = ("sales_invoice", "purchase_invoice", "intercompany_account_map")

# The db_path/conn fixtures below are SQLite-only (the setup conftest has no
# PostgreSQL branch); tests 1-2 therefore hold on SQLite only, and the live
# PostgreSQL pin is test_upgraded_install_matches_fresh_postgresql.
_requires_sqlite = pytest.mark.skipif(
    os.environ.get("ERPCLAW_DB_DIALECT") == "postgresql",
    reason="SQLite-fixture test; PostgreSQL is pinned by "
           "test_upgraded_install_matches_fresh_postgresql")
_EXPECTED_COLUMNS = ["sales_invoice.is_intercompany",
                     "sales_invoice.intercompany_reference_id",
                     "purchase_invoice.is_intercompany",
                     "purchase_invoice.intercompany_reference_id"]

# Fixed rewind statements: the only DDL text in this file. They return a
# fresh database to its genuine pre-044 shape (no linkage columns, no map
# table) while keeping every seeded row.
_REWIND_DROP_TABLE = "DROP TABLE IF EXISTS intercompany_account_map"
_REWIND_DROP_SI_IS = "ALTER TABLE sales_invoice DROP COLUMN is_intercompany"
_REWIND_DROP_SI_REF = "ALTER TABLE sales_invoice DROP COLUMN intercompany_reference_id"
_REWIND_DROP_PI_IS = "ALTER TABLE purchase_invoice DROP COLUMN is_intercompany"
_REWIND_DROP_PI_REF = "ALTER TABLE purchase_invoice DROP COLUMN intercompany_reference_id"
_REWIND_DROP_SI_DIM = "ALTER TABLE sales_invoice DROP COLUMN dimensions_json"
_REWIND_DROP_PI_DIM = "ALTER TABLE purchase_invoice DROP COLUMN dimensions_json"


def _shape(target):
    """The three tables' structure and constraints (None for an absent table)."""
    shape = {}
    for table in _TABLES:
        if seam.table_exists(table, target):
            shape[table] = (seam.describe_table(table, target),
                            seam.describe_constraints(table, target))
        else:
            shape[table] = None
    return shape


def _rewind(conn):
    """Drop the map table and the six columns; keep every row.

    The two `dimensions_json` columns ship on a fresh install (migration
    046), so a genuine pre-044 install never had them either: the rewind
    drops them too, and the upgrade runs 044 then 046 back to the fresh
    shape."""
    conn.execute(_REWIND_DROP_TABLE)
    conn.execute(_REWIND_DROP_SI_IS)
    conn.execute(_REWIND_DROP_SI_REF)
    conn.execute(_REWIND_DROP_PI_IS)
    conn.execute(_REWIND_DROP_PI_REF)
    conn.execute(_REWIND_DROP_SI_DIM)
    conn.execute(_REWIND_DROP_PI_DIM)
    conn.commit()
    seam.dispose_engines()


def _seed_company(conn):
    cid = str(uuid.uuid4())
    t = Table("company")
    conn.execute(
        Q.into(t).columns("id", "name", "abbr")
        .insert(P(), P(), P()).get_sql(),
        (cid, "Upgrade Co %s" % cid[:6], "UC%s" % cid[:4]))
    conn.commit()
    return cid


def _seed_customer(conn, company_id):
    cid = str(uuid.uuid4())
    t = Table("customer")
    conn.execute(
        Q.into(t).columns("id", "name", "company_id")
        .insert(P(), P(), P()).get_sql(),
        (cid, "Acme Corp", company_id))
    conn.commit()
    return cid


def _seed_invoice(conn, company_id, customer_id):
    si_id = str(uuid.uuid4())
    t = Table("sales_invoice")
    conn.execute(
        Q.into(t).columns("id", "customer_id", "posting_date", "company_id")
        .insert(P(), P(), P(), P()).get_sql(),
        (si_id, customer_id, "2026-06-20", company_id))
    conn.commit()
    return si_id


def _invoice_row(conn, si_id):
    t = Table("sales_invoice")
    row = conn.execute(
        Q.from_(t).select(t.star).where(t.id == P()).get_sql(),
        (si_id,)).fetchone()
    return dict(row)


def _do_upgraded(conn, target):
    """Shared body for the SQLite and PostgreSQL upgraded-install pins."""
    company_id = _seed_company(conn)
    customer_id = _seed_customer(conn, company_id)
    si_id = _seed_invoice(conn, company_id, customer_id)
    fresh_shape = _shape(target)
    old_row = _invoice_row(conn, si_id)
    assert "is_intercompany" in old_row

    _rewind(conn)
    assert "is_intercompany" not in seam.column_names("sales_invoice", target)
    assert not seam.table_exists("intercompany_account_map", target)

    once = mig.run_migration(target)
    assert once == {"columns_added": _EXPECTED_COLUMNS, "table_created": True}
    mig046.run_migration(target)
    assert _shape(target) == fresh_shape
    new_row = _invoice_row(conn, si_id)
    assert new_row == dict(old_row, is_intercompany=0,
                           intercompany_reference_id=None)

    again = mig.run_migration(target)
    assert again == {"columns_added": [], "table_created": False}


@_requires_sqlite
def test_fresh_install_has_the_schema(conn, db_path):
    for table, column in (("sales_invoice", "is_intercompany"),
                          ("sales_invoice", "intercompany_reference_id"),
                          ("purchase_invoice", "is_intercompany"),
                          ("purchase_invoice", "intercompany_reference_id")):
        assert column in seam.column_names(table, db_path)
    assert seam.table_exists("intercompany_account_map", db_path)
    before = _shape(db_path)
    assert mig.run_migration(db_path) == {"columns_added": [],
                                          "table_created": False}
    assert _shape(db_path) == before


@_requires_sqlite
def test_upgraded_install_matches_fresh(conn, db_path):
    _do_upgraded(conn, db_path)


def _load_init_schema():
    spec = importlib.util.spec_from_file_location("init_schema_pg_044",
                                                  _INIT_SCHEMA)
    schema_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(schema_mod)
    return schema_mod


@pytest.fixture
def pg_target(monkeypatch):
    """Live PostgreSQL target, reset per test (m62x pg-leg shape)."""
    pg_url = os.environ.get("ERPCLAW_PG_TEST_URL")
    if not pg_url:
        pytest.skip("ERPCLAW_PG_TEST_URL not set (live PostgreSQL required)")
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", pg_url)
    monkeypatch.delenv("ERPCLAW_DB_PATH", raising=False)
    expected_db = urlparse(pg_url).path.strip("/")
    if not expected_db:
        raise RuntimeError(
            "refusing to reset: ERPCLAW_PG_TEST_URL names no database")
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
    _load_init_schema().init_db(None)
    try:
        yield pg_url
    finally:
        seam.dispose_engines()


def test_upgraded_install_matches_fresh_postgresql(pg_target):
    conn = get_connection(pg_target)
    try:
        _do_upgraded(conn, pg_target)
    finally:
        conn.close()
