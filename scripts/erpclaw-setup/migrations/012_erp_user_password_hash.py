"""Migration 012: Add erp_user.password_hash column (BUG-005 fix).

`set-password` writes erp_user.password_hash, but the column was never created —
the action crashed. This adds it to existing DBs. This is the canonical example
of the "no column-migration path" gap (audit F4): previously there was no way to
add a column to an installed table, so the feature was parked behind xfail.

Plain ADD COLUMN (no rebuild, no FK-rewrite trap). Nullable (passwords are
optional; Telegram-auth users have none). Idempotent. Dialect-aware.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _sqlite_has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _table_exists_sqlite(conn, table):
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
    if _table_exists_sqlite(conn, "erp_user") and not _sqlite_has_column(conn, "erp_user", "password_hash"):
        conn.execute("ALTER TABLE erp_user ADD COLUMN password_hash TEXT")
        conn.commit()
        print("  erp_user.password_hash: added.")
    else:
        print("  erp_user.password_hash: already present (or erp_user absent).")
    conn.close()


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE erp_user ADD COLUMN IF NOT EXISTS password_hash TEXT")
        conn.commit()
        print("  Postgres: erp_user.password_hash ensured.")
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
    parser = argparse.ArgumentParser(description="Migration 012: erp_user.password_hash column")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 012 complete.")
