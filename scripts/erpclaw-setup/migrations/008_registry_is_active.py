"""Migration 008: Add is_active to the type/status registries.

Wave 0 / M0 additive slice. The registry-CRUD `deactivate-*-type` actions soft-
disable a registered type by setting is_active=0; the enforcement reads
(add-account, gl_posting, stock_posting, add-payment, update-asset) require
is_active=1. This adds the column to existing DBs.

Plain ADD COLUMN (no table rebuild) — safe, and NOT subject to the
ALTER-TABLE-RENAME FK-rewrite trap (migrations 003-007). NOT NULL DEFAULT 1, so
existing rows become active. Idempotent. Dialect-aware.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

_REGISTRIES = [
    "voucher_type_registry",
    "party_type_registry",
    "account_type_registry",
    "asset_status_registry",
]


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
    added = []
    for t in _REGISTRIES:
        if _table_exists_sqlite(conn, t) and not _sqlite_has_column(conn, t, "is_active"):
            conn.execute(
                f"ALTER TABLE {t} ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1 "
                f"CHECK(is_active IN (0,1))"
            )
            added.append(t)
    conn.commit()
    conn.close()
    print(f"  is_active added to: {', '.join(added) if added else '(none — already present)'}")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for t in _REGISTRIES:
                cur.execute(
                    f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS is_active "
                    f"INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1))"
                )
        conn.commit()
        print("  Postgres: is_active ensured on all four registries.")
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
    parser = argparse.ArgumentParser(description="Migration 008: registry is_active column")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 008 complete.")
