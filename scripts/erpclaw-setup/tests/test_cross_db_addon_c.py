"""Live-Postgres regression test for Wave 0 cross-DB add-on C (ERP-28).

Add-on C re-runs the M1/S2/M8/S8 runtime against Postgres and migrates the
SQLite-only SQL those paths used. The two things that genuinely fail on
Postgres without this work and so are proven here against a live server:

  1. The ``decimal_sum(text)`` aggregate. On SQLite it is a per-connection
     Python aggregate (``conn.create_aggregate``); on Postgres it must be a
     persistent SQL aggregate. ``db._ensure_pg_decimal_sum`` (run inside
     ``get_connection``) creates it. S2's ``get-payments-outstanding`` /
     ``allocate-payment`` FIFO paths sum TEXT-stored amounts with it.

  2. The dialect-portable SQL that replaced SQLite-only constructs:
       - ``now()``                          (was ``datetime('now')``)
       - ``CAST(col AS NUMERIC) > 0``        (was ``col + 0 > 0``)
       - ``CAST(decimal_sum(c) AS NUMERIC)`` (was ``decimal_sum(c) + 0``)
       - ``ORDER BY id``                     (was ``ORDER BY rowid``)
     Each must parse and run on Postgres; ``col + 0``/``rowid``/``datetime()``
     would raise there.

GATED: skipped unless ``ERPCLAW_PG_TEST_URL`` points at a reachable, EXPENDABLE
Postgres database (it creates/drops a throwaway table in ``public``). CI has no
Postgres, so this stays skipped there; run it on the OpenClaw PG box:

    ERPCLAW_PG_TEST_URL='postgresql://erpclaw@localhost/erpclaw_test' \
        pytest source/erpclaw/scripts/erpclaw-setup/tests/test_cross_db_addon_c.py
"""
import os
import sys
from decimal import Decimal

import pytest

ERPCLAW_LIB = os.path.expanduser("~/.openclaw/erpclaw/lib")
if ERPCLAW_LIB not in sys.path:
    sys.path.insert(0, ERPCLAW_LIB)

PG_URL = os.environ.get("ERPCLAW_PG_TEST_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="ERPCLAW_PG_TEST_URL not set (live Postgres required for add-on C)",
)


@pytest.fixture
def pg(monkeypatch):
    """Yield a dialect-aware get_connection() bound to the live PG, with a
    throwaway ``addon_c_amt`` table holding TEXT-stored decimal amounts."""
    monkeypatch.setenv("ERPCLAW_DB_DIALECT", "postgresql")
    monkeypatch.setenv("ERPCLAW_DB_URL", PG_URL)

    from erpclaw_lib.db import get_connection
    conn = get_connection()  # ensures the decimal_sum aggregate exists
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS addon_c_amt")
    cur.execute(
        "CREATE TABLE addon_c_amt ("
        "  id TEXT PRIMARY KEY,"
        "  amount TEXT NOT NULL DEFAULT '0',"
        "  unallocated_amount TEXT NOT NULL DEFAULT '0',"
        "  updated_at TEXT)"
    )
    conn.commit()
    cur.close()
    yield conn
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS addon_c_amt")
    conn.commit()
    cur.close()
    conn.close()


def test_decimal_sum_aggregate_is_exact_on_postgres(pg):
    """decimal_sum sums TEXT amounts with Decimal precision (no float drift)."""
    pg.execute("INSERT INTO addon_c_amt (id, amount) VALUES (?, ?)", ("a", "0.1"))
    pg.execute("INSERT INTO addon_c_amt (id, amount) VALUES (?, ?)", ("b", "0.2"))
    pg.execute("INSERT INTO addon_c_amt (id, amount) VALUES (?, ?)", ("c", "1000000.07"))
    pg.commit()

    row = pg.execute("SELECT decimal_sum(amount) AS total FROM addon_c_amt").fetchone()
    total = Decimal(str(row["total"]))
    # 0.1 + 0.2 + 1000000.07 == 1000000.37 exactly; float SUM would drift.
    assert total == Decimal("1000000.37"), f"got {row['total']!r}"


def test_decimal_sum_empty_set_coalesces_to_zero(pg):
    """An empty group + COALESCE(..., '0') yields '0' (matches the SQLite path)."""
    row = pg.execute(
        "SELECT COALESCE(decimal_sum(amount), '0') AS total "
        "FROM addon_c_amt WHERE id = 'nope'"
    ).fetchone()
    assert Decimal(str(row["total"])) == Decimal("0")


def test_cast_numeric_filter_runs_on_postgres(pg):
    """The CAST(col AS NUMERIC) > 0 filter (replacing col + 0 > 0) executes."""
    pg.execute(
        "INSERT INTO addon_c_amt (id, unallocated_amount) VALUES (?, ?)", ("x", "5.00"))
    pg.execute(
        "INSERT INTO addon_c_amt (id, unallocated_amount) VALUES (?, ?)", ("y", "0"))
    pg.commit()
    rows = pg.execute(
        "SELECT id FROM addon_c_amt "
        "WHERE CAST(unallocated_amount AS NUMERIC) > 0 ORDER BY id"
    ).fetchall()
    assert [r["id"] for r in rows] == ["x"]


def test_cast_decimal_sum_having_runs_on_postgres(pg):
    """HAVING CAST(decimal_sum(c) AS NUMERIC) != 0 parses + runs on PG."""
    pg.execute("INSERT INTO addon_c_amt (id, amount) VALUES (?, ?)", ("p", "3.33"))
    pg.commit()
    rows = pg.execute(
        "SELECT id, decimal_sum(amount) AS s FROM addon_c_amt "
        "GROUP BY id HAVING CAST(decimal_sum(amount) AS NUMERIC) != 0"
    ).fetchall()
    assert ("p", Decimal("3.33")) in [(r["id"], Decimal(str(r["s"]))) for r in rows]


def test_m1_custom_field_value_upsert_round_trip_on_postgres(pg):
    """M1's EAV store uses INSERT ... ON CONFLICT(...) DO UPDATE SET
    value = excluded.value — valid on both SQLite and Postgres. Prove the
    upsert + fetch round-trip works against a live PG."""
    from erpclaw_lib import custom_fields

    cur = pg.cursor()
    cur.execute("DROP TABLE IF EXISTS custom_field_value")
    cur.execute(
        "CREATE TABLE custom_field_value ("
        "  table_name TEXT NOT NULL,"
        "  doc_id     TEXT NOT NULL,"
        "  field_name TEXT NOT NULL,"
        "  value      TEXT,"
        "  PRIMARY KEY (table_name, doc_id, field_name))"
    )
    pg.commit()
    cur.close()

    # First write
    custom_fields.store_custom_field_values(pg, "customer", "cust-1", {"region": "West"})
    pg.commit()
    assert custom_fields.fetch_custom_field_values(pg, "customer", "cust-1") == {"region": "West"}

    # Conflicting write on the same key updates in place (the ON CONFLICT path)
    custom_fields.store_custom_field_values(pg, "customer", "cust-1", {"region": "East"})
    pg.commit()
    assert custom_fields.fetch_custom_field_values(pg, "customer", "cust-1") == {"region": "East"}

    cur = pg.cursor()
    cur.execute("DROP TABLE IF EXISTS custom_field_value")
    pg.commit()
    cur.close()


def test_now_helper_renders_and_runs_on_postgres(pg):
    """query.now() renders NOW()::text on PG and an UPDATE using it succeeds."""
    from erpclaw_lib.query import now, dynamic_update
    from erpclaw_lib.vendor.pypika.terms import LiteralValue

    assert isinstance(now(), LiteralValue)
    assert "NOW()" in str(now())  # dialect-aware: not datetime('now')

    pg.execute("INSERT INTO addon_c_amt (id, amount) VALUES (?, ?)", ("t", "1"))
    pg.commit()
    sql, params = dynamic_update(
        "addon_c_amt", {"amount": "2", "updated_at": now()}, {"id": "t"})
    pg.execute(sql, params)
    pg.commit()
    row = pg.execute(
        "SELECT amount, updated_at FROM addon_c_amt WHERE id = ?", ("t",)).fetchone()
    assert row["amount"] == "2"
    assert row["updated_at"] is not None
