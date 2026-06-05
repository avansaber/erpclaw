"""Migration 016: Add email + phone columns to customer and supplier (FINDING-003).

customer and supplier carried only a generic free-text primary_contact line and
no structured email/phone. The gateway agent improvised JSON blobs into
primary_contact, breaking dunning's _resolve_customer_email and the import-*
INSERTs (which already name email/phone). This adds the four columns:

  - customer.email TEXT, customer.phone TEXT
  - supplier.email TEXT, supplier.phone TEXT

primary_contact is kept (distinct free-text purpose). Columns are nullable and
store verbatim (no format validation). Plain ADD COLUMN (no rebuild).
Idempotent. Dialect-aware.

ADR: internal/decisions/ADR-0012-customer-supplier-email-phone-columns.md
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")
_COLUMNS = [
    ("customer", "email"),
    ("customer", "phone"),
    ("supplier", "email"),
    ("supplier", "phone"),
]


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
    for table, column in _COLUMNS:
        if _sqlite_has_table(conn, table) and not _sqlite_has_column(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            conn.commit()
            print(f"  {table}.{column}: added.")
        else:
            print(f"  {table}.{column}: already present (or table absent).")
    conn.close()


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for table, column in _COLUMNS:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} TEXT")
                print(f"  Postgres: {table}.{column} ensured.")
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
    parser = argparse.ArgumentParser(description="Migration 016: customer/supplier email+phone")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 016 complete.")
