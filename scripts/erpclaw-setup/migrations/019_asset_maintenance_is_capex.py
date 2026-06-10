"""Migration 019: M7 asset depth — asset_maintenance.is_capex column + backfill.

Adds the capex-vs-opex flag to asset_maintenance. When is_capex=1,
complete-maintenance capitalizes the maintenance cost into the asset
(DR Asset / CR Cash) and recomputes the depreciation schedule; when 0 it posts
the cost as a repair expense (DR Repair / CR Cash). Existing rows pre-date the
distinction and are repairs, so they are backfilled to 0 (opex).

The column is added NOT NULL DEFAULT 0 (so the ADD COLUMN itself sets every
existing row to 0); the explicit UPDATE is a belt-and-braces backfill that also
covers any row a prior partial run left NULL. Plain ADD COLUMN (no rebuild).
Idempotent. Dialect-aware. Pairs with migration 018.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

_TABLE = "asset_maintenance"
_COLUMN = "is_capex"
# Fully-literal SQL (table + column are fixed constants — no interpolation, so no
# injection surface and no f-string scanner flag). DDL matches init_schema exactly.
_SQLITE_ADD = "ALTER TABLE asset_maintenance ADD COLUMN is_capex INTEGER NOT NULL DEFAULT 0 CHECK(is_capex IN (0,1))"
_PG_ADD = "ALTER TABLE asset_maintenance ADD COLUMN IF NOT EXISTS is_capex INTEGER NOT NULL DEFAULT 0 CHECK(is_capex IN (0,1))"
_BACKFILL = "UPDATE asset_maintenance SET is_capex = 0 WHERE is_capex IS NULL"


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _sqlite_has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _sqlite_has_table(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _run_sqlite(path):
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")
    if not _sqlite_has_table(conn, _TABLE):
        print(f"  {_TABLE}: table absent. Nothing to migrate.")
        conn.close()
        return
    if not _sqlite_has_column(conn, _TABLE, _COLUMN):
        conn.execute(_SQLITE_ADD)
        print(f"  {_TABLE}.{_COLUMN}: added.")
    else:
        print(f"  {_TABLE}.{_COLUMN}: already present.")
    # Explicit backfill: any pre-existing / NULL row is an opex repair.
    cur = conn.execute(_BACKFILL)
    print(f"  {_TABLE}.{_COLUMN}: backfilled {cur.rowcount} NULL row(s) to 0 (opex).")
    conn.commit()
    conn.close()


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(_PG_ADD)
            print(f"  Postgres: {_TABLE}.{_COLUMN} ensured.")
            cur.execute(_BACKFILL)
            print(f"  Postgres: {_TABLE}.{_COLUMN} backfilled {cur.rowcount} NULL row(s) to 0.")
        conn.commit()
    finally:
        conn.close()


def run_migration(db_path=None):
    if _get_dialect() == "postgresql":
        url = os.environ.get("ERPCLAW_DB_URL") or db_path
        if not url:
            print("Postgres dialect set but no connection URL (ERPCLAW_DB_URL). Nothing to migrate.")
            return
        _run_postgres(url)
        return
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    if not os.path.exists(path):
        print(f"Database not found at {path}. Nothing to migrate.")
        return
    _run_sqlite(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migration 019: asset_maintenance.is_capex")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 019 complete.")
