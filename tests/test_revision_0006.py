"""Revision 0006 on older books: every row kept, the date order unset, the books' identity made once."""
from __future__ import annotations

import re

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import DBAPIError

from conftest import drop_everything
from erpclaw_core.db import OWNER_MARK, create_all
from erpclaw_core.migrate import upgrade

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NOW = "2026-10-26 12:00:00+00:00"


def books_id(conn) -> str:
    """The books' identity, read by the test itself: exactly one row."""
    return conn.execute(text("SELECT books_id FROM books_identity")).scalar_one()


def _older_books(conn) -> None:
    """Books as an engine at revision 0005 left them: its mark, two companies, a customer, an item."""
    upgrade(conn, "0005")
    conn.execute(text("INSERT INTO erpclaw_books VALUES (:m)"), {"m": OWNER_MARK})
    for cid, name, cur in (("c1", "Old Co", "USD"), ("c2", "Old Ltd", "GBP")):
        conn.execute(text("INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, created_at) "
                          "VALUES (:id, :n, :c, 4, :now)"), {"id": cid, "n": name, "c": cur, "now": NOW})
    conn.execute(text("INSERT INTO party (id, company_id, kind, name, email, city) "
                      "VALUES ('p1', 'c1', 'customer', 'Acme Corp', 'ap@acme.test', 'Denver')"))
    conn.execute(text("INSERT INTO item (id, company_id, name, unit, standard_rate) "
                      "VALUES ('i1', 'c1', 'Bolt', 'unit', '2.50')"))


def _rows(conn) -> dict[str, list[dict]]:
    """Every row of every table, in a fixed order."""
    out = {}
    for name in sorted(inspect(conn).get_table_names()):
        rows = [dict(r) for r in conn.execute(text(f'SELECT * FROM "{name}"')).mappings()]
        out[name] = sorted(rows, key=lambda r: sorted((k, str(v)) for k, v in r.items()))
    return out


def test_older_books_keep_every_row_stay_unset_and_get_one_identity(engine):
    drop_everything(engine)
    with engine.begin() as conn:
        _older_books(conn)
        before = _rows(conn)
    with engine.begin() as conn:
        upgrade(conn, "0006")  # this revision, whatever later ones add
        after = _rows(conn)
        assert conn.execute(text("SELECT date_order FROM company ORDER BY id")).scalars().all() == [None, None]
    del before["alembic_version"], after["alembic_version"]
    identity = after.pop("books_identity")
    assert [{k: v for k, v in r.items() if k != "date_order"} for r in after.pop("company")] == before.pop("company")
    assert after == before  # every other row exactly as it was
    assert len(identity) == 1 and identity[0]["id"] == 1 and UUID.match(identity[0]["books_id"])
    first = identity[0]["books_id"]

    with engine.begin() as conn:  # Reapplying this historical revision keeps the identity.
        upgrade(conn, "0006")
    with engine.connect() as conn:
        before_init = _rows(conn)
    with pytest.raises(DBAPIError, match="older books are disposable"):
        create_all(engine)  # Current initialization refuses old books instead of rewriting their chain.
    with engine.connect() as conn:
        assert _rows(conn) == before_init
        assert books_id(conn) == first
        assert conn.execute(text("SELECT count(*) FROM books_identity")).scalar_one() == 1


def test_older_books_with_a_company_but_no_mark_still_get_an_identity(engine):
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn, "0005")
        conn.execute(text("INSERT INTO company (id, name, functional_currency, fiscal_year_start_month, created_at) "
                          "VALUES ('c1', 'Old Co', 'USD', 1, :now)"), {"now": NOW})
    with engine.begin() as conn:
        upgrade(conn, "0006")
        assert UUID.match(books_id(conn))


def test_an_empty_database_gets_its_identity_from_init_not_from_the_revision(engine):
    """A restore's staging database is upgraded empty: the revision writes no identity there, so the backup's own
    is the one restored; init writes one for new books."""
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn)
        assert conn.execute(text("SELECT count(*) FROM books_identity")).scalar_one() == 0
    create_all(engine)
    with engine.connect() as conn:
        assert UUID.match(books_id(conn))


def test_fresh_head_init_is_idempotent_and_keeps_books_identity(engine):
    drop_everything(engine)
    create_all(engine)
    with engine.connect() as conn:
        first = books_id(conn)
        assert UUID.match(first)
    create_all(engine)
    with engine.connect() as conn:
        assert books_id(conn) == first
        assert conn.execute(text("SELECT count(*) FROM books_identity")).scalar_one() == 1


@pytest.mark.parametrize("sql", ["UPDATE books_identity SET books_id = '00000000-0000-4000-8000-000000000000'",
                                 "DELETE FROM books_identity", "TRUNCATE books_identity"])
def test_the_identity_never_changes_even_for_the_owner(engine, sql):
    with engine.connect() as conn:
        first = books_id(conn)
    with pytest.raises(DBAPIError, match="identity is written once"):
        with engine.begin() as conn:
            conn.execute(text(sql))
    with engine.connect() as conn:
        assert books_id(conn) == first


@pytest.mark.parametrize("row", ["(2, '00000000-0000-4000-8000-000000000000')", "(1, 'not-a-uuid')"])
def test_a_second_identity_or_one_of_another_shape_is_refused(engine, row):
    drop_everything(engine)
    with engine.begin() as conn:
        upgrade(conn)
    with pytest.raises(DBAPIError, match="ck_books_identity"):
        with engine.begin() as conn:
            conn.execute(text(f"INSERT INTO books_identity (id, books_id) VALUES {row}"))


def test_the_date_order_allows_only_mdy_dmy_or_unset(engine):
    with engine.begin() as conn:
        for value in ("MDY", "DMY", None):
            conn.execute(text("UPDATE company SET date_order = :v"), {"v": value})
    for value in ("YMD", "mdy", ""):
        with pytest.raises(DBAPIError, match="ck_company_date_order"):
            with engine.begin() as conn:
                conn.execute(text("UPDATE company SET date_order = :v"), {"v": value})
