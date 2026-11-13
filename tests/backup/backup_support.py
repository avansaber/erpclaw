"""Shared by the service backup tests: new databases for restore (removed afterwards), older books, and the check that
nothing was published."""
from __future__ import annotations

import secrets
import uuid

import pytest
from sqlalchemy import MetaData, create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from conftest import PG_URL, drop_everything
from erpclaw_core import backup as bk, files
from erpclaw_core.db import OWNER_MARK, make_engine
from erpclaw_core.migrate import upgrade

ADMIN = make_url(PG_URL).set(database="postgres")
NOW = "2026-10-26 12:00:00+00:00"
_MADE: list[str] = []


def new_target(runtime_role=None) -> bk.NewPostgresDatabase:
    name = f"restored_{secrets.token_hex(4)}_test"
    _MADE.append(name)
    return bk.NewPostgresDatabase(PG_URL, name, runtime_role)


def open_target(target: bk.NewPostgresDatabase):
    return make_engine(make_url(target.server_url).set(database=target.name).render_as_string(hide_password=False))


def databases(pattern: str) -> list[str]:
    admin = create_engine(ADMIN, poolclass=NullPool)
    try:
        with admin.connect() as c:
            return list(c.exec_driver_sql("SELECT datname FROM pg_database WHERE datname LIKE %(p)s",
                                          {"p": pattern}).scalars())
    finally:
        admin.dispose()


def nothing_published(target: bk.NewPostgresDatabase) -> None:
    """No database under the final name, no staging database left, no file root and no file staging."""
    assert databases(target.name) == []
    assert databases("erpclaw\\_restore\\_%") == []
    assert not (files.files_dir() / files.root_name(target.name)).exists()
    base = files.files_dir()
    assert not (base.is_dir() and list(base.glob(f"{files.RESTORE_PREFIX}*")))


def drop_made() -> None:
    admin = create_engine(ADMIN, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as c:
            names = _MADE + list(c.exec_driver_sql(
                "SELECT datname FROM pg_database WHERE datname LIKE 'erpclaw_restore_%%'").scalars())
            for n in names:
                c.exec_driver_sql(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
    finally:
        admin.dispose()
    _MADE.clear()


@pytest.fixture
def pg_new():
    """New PostgreSQL databases for restore, each removed when the test ends."""
    yield new_target
    drop_made()


def older_books(engine, core: str = "0004", *, populated: bool = True) -> None:
    """Books as an engine at an older core revision (0004 by default) with the sales plugin's first revision left
    them: the mark, two companies, a customer, an item and the sales plugin's install record at an older version.
    With populated=False, make only the schema and its seeded currencies: no ownership mark or books identity.
    Those alone would make even books with no companies ineligible for the ledger-format migration.
    """
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, core)
        upgrade(conn, "sales_0001")
        if not populated:
            return
        conn.execute(text("INSERT INTO erpclaw_books VALUES (:m)"), {"m": OWNER_MARK})
        if core >= "0006":
            conn.execute(text("INSERT INTO books_identity VALUES (1, :id)"), {"id": str(uuid.uuid4())})
        for cid, name, cur in (("c1", "Old Co", "USD"), ("c2", "Old Ltd", "GBP")):
            conn.execute(text("INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, "
                              "created_at) VALUES (:id, :n, :c, 4, :now)"),
                         {"id": cid, "n": name, "c": cur, "now": NOW})
            conn.execute(text("INSERT INTO posting_chain_head (company_id, last_seq, last_hash) VALUES (:id, 0, :g)"),
                         {"id": cid, "g": "0" * 64})  # an empty chain, as a new company's
        conn.execute(text("INSERT INTO party (id, company_id, kind, name, email, city) "
                          "VALUES ('p1', 'c1', 'customer', 'Acme Corp', 'ap@acme.test', 'Denver')"))
        conn.execute(text("INSERT INTO item (id, company_id, name, unit, standard_rate) "
                          "VALUES ('i1', 'c1', 'Bolt', 'unit', '2.50')"))
        conn.execute(text("INSERT INTO plugin_install (name, version, kind, installed_at, manifest_digest, "
                          "artifact_digest, after_core_revision, status) VALUES ('sales', '0.0.0', "
                          "'business-module', :now, :d, :d, '0004', 'installed')"), {"now": NOW, "d": "0" * 64})


def snapshot(engine_or_conn) -> dict[str, list[dict]]:
    """Independent, physical row comparison, including every text money value and every control table."""
    if hasattr(engine_or_conn, "connect"):
        with engine_or_conn.connect() as conn:
            return snapshot(conn)
    conn = engine_or_conn
    schema = MetaData()
    schema.reflect(conn)
    return {t.name: [dict(r) for r in conn.execute(select(t).order_by(*t.primary_key.columns)).mappings()]
            for t in schema.tables.values()}


def upgradeable_books(engine, core="0015", sales="sales_0002", stock=None) -> None:
    """Real demo history in a schema made at a supported older revision, with its own column lists.

    Keep the requested plugin branches. The old schema's constraints remain active; plugin transition triggers are
    suspended while copying the fixture's state, as opposed to replaying its business operations.
    """
    source = snapshot(engine)
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, core)
        upgrade(conn, sales)
        if stock:
            upgrade(conn, stock)
        schema = MetaData()
        schema.reflect(conn)
        for t in schema.sorted_tables:
            if t.name in ("alembic_version", "currency"):
                continue
            rows = [{c.name: r[c.name] for c in t.columns} for r in source[t.name]]
            if t.name == "plugin_install":
                rows = [dict(r, version="0.0.0", manifest_digest="0" * 64, artifact_digest="0" * 64)
                        for r in rows if r["name"] in ({"sales", "stock"} if stock else {"sales"})]
            if t.name == "account" and core < "0015":
                rows = [r for r in rows if r["account_type"] not in
                        ("opening_balance", "stock_adjustment", "stock_write_off")]
            plugin = t.name.startswith(("sales_", "stock_"))
            if plugin:
                conn.exec_driver_sql(f'ALTER TABLE "{t.name}" DISABLE TRIGGER USER')
            # A FIFO root layer names itself as its group. Copy roots before the other layers of each group.
            ordered = (sorted(rows, key=lambda r: (r["group_id"] != r["id"], r["id"]))
                       if t.name == "stock_layer" else bk._restore_order(t, rows))
            for row in ordered:
                conn.execute(t.insert().values(**row))
            if plugin:
                conn.exec_driver_sql(f'ALTER TABLE "{t.name}" ENABLE TRIGGER USER')
