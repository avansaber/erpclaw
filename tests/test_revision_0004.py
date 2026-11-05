"""Revision 0004: the core tables create_all still made come under migration."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from conftest import drop_everything
from erpclaw_core.migrate import upgrade

OLD_ITEM = ("CREATE TABLE item (id VARCHAR(36) NOT NULL PRIMARY KEY, company_id VARCHAR(36) NOT NULL REFERENCES company(id), "
            "name VARCHAR(200) NOT NULL, unit VARCHAR(20) NOT NULL, standard_rate VARCHAR(40) NOT NULL, "
            "UNIQUE (company_id, name))")
COMPANY = ("INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, created_at) "
           "VALUES ('c1', 'Old Co', 'USD', 1, CURRENT_TIMESTAMP)")


def test_fresh_books_get_all_eight_tables_from_the_migration(engine):
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0004")
        names = set(inspect(conn).get_table_names())
    assert {"erpclaw_books", "plugin_install", "number_series", "audit_log", "approval", "operation", "outbox",
            "item"} <= names


def test_older_books_keep_their_rows_and_item_gains_its_rules(engine):
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0003")
        conn.exec_driver_sql(OLD_ITEM)  # as create_all made it before 0004
        conn.exec_driver_sql(COMPANY)
        conn.exec_driver_sql("INSERT INTO item VALUES ('i1', 'c1', 'Bolt', 'unit', '2.50')")
    with engine.begin() as conn:
        upgrade(conn, "0004")  # Exercise this historical revision before the later format-2 refusal.
        assert conn.exec_driver_sql("SELECT standard_rate FROM item WHERE id = 'i1'").scalar_one() == "2.50"
        uniques = {u["name"] for u in inspect(conn).get_unique_constraints("item")}
        assert "uq_item_company_id" in uniques
    with pytest.raises(DBAPIError):  # the rate rule now holds
        with engine.begin() as conn:
            conn.exec_driver_sql("INSERT INTO item VALUES ('i2', 'c1', 'Nut', 'unit', '0.00')")


def _create_all_made(conn, name):
    """The table as create_all made it before 0004: 0004's own frozen definition (the models move on)."""
    import importlib.util

    import sqlalchemy as sa

    from erpclaw_core.migrate import CORE_VERSIONS

    spec = importlib.util.spec_from_file_location("rev0004", CORE_VERSIONS / "0004_core_tables_by_migration.py")
    rev = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rev)
    md = sa.MetaData()
    md.reflect(conn)  # what it references (company, approval) is already in these books
    sa.Table(name, md, *rev._tables()[name]).create(conn)


@pytest.mark.parametrize("change, what", [
    ("ALTER TABLE plugin_install ADD COLUMN extra INTEGER", "columns"),
    (None, "checks"),  # the same columns, its status check missing
    (None, "unique keys"),  # the same columns, approval's operation key not unique
    ("predicate", "checks"),  # ck_approval_status by name, with a different predicate
])
def test_an_older_table_that_differs_is_refused_naming_it(engine, change, what):
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0003")
        if what == "columns":
            _create_all_made(conn, "plugin_install")
            conn.exec_driver_sql(change)
            table = "plugin_install"
        elif change == "predicate":
            ts = "TIMESTAMP WITH TIME ZONE"
            conn.exec_driver_sql(
                f"CREATE TABLE approval (id VARCHAR(36) NOT NULL PRIMARY KEY, company_id VARCHAR(36) NOT NULL "
                f"REFERENCES company(id), conversation_id VARCHAR(36), contract VARCHAR(100) NOT NULL, args_json TEXT "
                f"NOT NULL, digest VARCHAR(64) NOT NULL, card_json TEXT NOT NULL, status VARCHAR(12) NOT NULL, "
                f"operation_key VARCHAR(36) NOT NULL, created_at {ts} NOT NULL, expires_at {ts} NOT NULL, "
                f"decided_at {ts}, UNIQUE (operation_key), "
                f"CONSTRAINT ck_approval_status CHECK (status in ('awaiting','approved')))")
            table = "approval"
        elif what == "checks":
            conn.exec_driver_sql(
                "CREATE TABLE outbox (id VARCHAR(36) NOT NULL PRIMARY KEY, company_id VARCHAR(36) NOT NULL "
                "REFERENCES company(id), operation_key VARCHAR(36), slot VARCHAR(40) NOT NULL, recipient VARCHAR(200) "
                "NOT NULL, subject VARCHAR(300) NOT NULL, body TEXT NOT NULL, status VARCHAR(12) NOT NULL, "
                "created_at TIMESTAMP WITH TIME ZONE NOT NULL, delivered_at TIMESTAMP WITH TIME ZONE, detail TEXT)")
            table = "outbox"
        else:
            _create_all_made(conn, "approval")
            conn.exec_driver_sql("DROP TABLE approval")
            ts = "TIMESTAMP WITH TIME ZONE"
            conn.exec_driver_sql(
                f"CREATE TABLE approval (id VARCHAR(36) NOT NULL PRIMARY KEY, company_id VARCHAR(36) NOT NULL "
                f"REFERENCES company(id), conversation_id VARCHAR(36), contract VARCHAR(100) NOT NULL, args_json TEXT "
                f"NOT NULL, digest VARCHAR(64) NOT NULL, card_json TEXT NOT NULL, status VARCHAR(12) NOT NULL, "
                f"operation_key VARCHAR(36) NOT NULL, created_at {ts} NOT NULL, expires_at {ts} NOT NULL, "
                f"decided_at {ts}, CONSTRAINT ck_approval_status CHECK (status in ('awaiting','approved','rejected',"
                f"'expired','superseded','committed','failed')))")
            table = "approval"
    with pytest.raises(RuntimeError, match=f"{table} table was made by an earlier build .* in its {what}"):
        with engine.begin() as conn:
            upgrade(conn)


def test_tables_create_all_made_are_adopted(engine):
    """Older books whose tables create_all made exactly as the models declare upgrade without change."""
    from erpclaw_core.db import metadata

    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0003")
        conn.exec_driver_sql(OLD_ITEM)
        for name in ("erpclaw_books", "plugin_install", "number_series", "audit_log", "approval", "operation",
                     "outbox"):
            _create_all_made(conn, name)
    with engine.begin() as conn:
        upgrade(conn, "0004")
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == "0004"


def test_a_rolled_back_transaction_keeps_no_table(engine):
    """DDL is inside the transaction, so a migration or install that fails after its CREATE TABLE leaves nothing."""
    with pytest.raises(RuntimeError, match="forced"):
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE probe_rolled_back (x INTEGER)")
            raise RuntimeError("forced")
    assert "probe_rolled_back" not in inspect(engine).get_table_names()
