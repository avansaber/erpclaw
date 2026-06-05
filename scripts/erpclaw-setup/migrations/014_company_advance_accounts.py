"""Migration 014: Add company advance-account columns (Wave 0 / S2).

B1-style advance payments: when set, submit-payment routes the unallocated
advance leg to a dedicated "Advance from Customer" (liability) / "Advance to
Supplier" (asset) sub-account instead of the AR/AP control account. Nullable —
backward-compatible (unset = current behavior). Adds the columns to existing DBs.

Plain ADD COLUMN (no rebuild). Idempotent. Dialect-aware.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

_COLUMNS = [
    ("advance_from_customer_account_id", "TEXT REFERENCES account(id) ON DELETE RESTRICT"),
    ("advance_to_supplier_account_id", "TEXT REFERENCES account(id) ON DELETE RESTRICT"),
]


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
    added = []
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='company'").fetchone():
        for col, coldef in _COLUMNS:
            if not _sqlite_has_column(conn, "company", col):
                # SQLite can't ADD COLUMN with a REFERENCES to an existing table via
                # inline FK only if it has a non-constant default; these are nullable
                # with no default, so the plain ADD COLUMN is accepted.
                conn.execute(f"ALTER TABLE company ADD COLUMN {col} {coldef}")
                added.append(col)
    conn.commit()
    conn.close()
    print(f"  company advance-account columns added: {', '.join(added) if added else '(none — already present)'}")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for col, coldef in _COLUMNS:
                cur.execute(f"ALTER TABLE company ADD COLUMN IF NOT EXISTS {col} {coldef}")
        conn.commit()
        print("  Postgres: company advance-account columns ensured.")
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
    parser = argparse.ArgumentParser(description="Migration 014: company advance-account columns")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 014 complete.")
