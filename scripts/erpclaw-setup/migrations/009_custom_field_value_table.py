"""Migration 009: Create the custom_field_value EAV table (M1 — UDF runtime).

Wave 0 / M1. The custom-field runtime (erpclaw_lib.custom_fields) was shipped
but inert: the custom_field_value table it reads/writes was never created in any
init_schema / init_db / migration. This adds it to existing DBs.

Column names + the composite key match the shipped lib exactly (doc_id, and
ON CONFLICT (table_name, doc_id, field_name)). No surrogate id column: the lib's
INSERT does not populate one, so a PRIMARY KEY id would break under Postgres.

Pure CREATE TABLE / CREATE INDEX IF NOT EXISTS — no rebuild, no FK-rewrite trap.
Idempotent. Dialect-aware.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

_DDL = [
    """CREATE TABLE IF NOT EXISTS custom_field_value (
        table_name      TEXT NOT NULL,
        doc_id          TEXT NOT NULL,
        field_name      TEXT NOT NULL,
        value           TEXT,
        created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (table_name, doc_id, field_name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cfv_table_field "
    "ON custom_field_value(table_name, field_name)",
]


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _run_sqlite(path):
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='custom_field_value'"
    ).fetchone() is not None
    for stmt in _DDL:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    print(f"  custom_field_value: {'already present' if existed else 'created'} (+ index).")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for stmt in _DDL:
                cur.execute(stmt)
        conn.commit()
        print("  Postgres: custom_field_value ensured (+ index).")
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
    parser = argparse.ArgumentParser(description="Migration 009: custom_field_value table")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 009 complete.")
