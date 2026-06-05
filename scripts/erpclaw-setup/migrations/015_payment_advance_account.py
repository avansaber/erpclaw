"""Migration 015: Add payment_entry.advance_account_id (Wave 0 / S2 phase 2).

When submit-payment routes the unallocated (advance) portion to a dedicated
advance liability/asset sub-account, the account is recorded here so
allocate-payment can post the offsetting reclassification. NULL = not routed
(legacy AR/AP-control behavior). Adds the column to existing DBs.

Plain ADD COLUMN (no rebuild). Idempotent. Dialect-aware.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")
_COLDEF = "TEXT REFERENCES account(id) ON DELETE RESTRICT"


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _sqlite_has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _run_sqlite(path):
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")
    if (conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='payment_entry'").fetchone()
            and not _sqlite_has_column(conn, "payment_entry", "advance_account_id")):
        conn.execute(f"ALTER TABLE payment_entry ADD COLUMN advance_account_id {_COLDEF}")
        conn.commit()
        print("  payment_entry.advance_account_id: added.")
    else:
        print("  payment_entry.advance_account_id: already present (or table absent).")
    conn.close()


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE payment_entry ADD COLUMN IF NOT EXISTS advance_account_id {_COLDEF}")
        conn.commit()
        print("  Postgres: payment_entry.advance_account_id ensured.")
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
    parser = argparse.ArgumentParser(description="Migration 015: payment_entry.advance_account_id")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 015 complete.")
