"""Migration 017: Create the dimension_registry table (M6 multi-dimensional GL).

The accounting-dimensions registry drives which keys may appear in
``gl_entry.dimensions_json``, their data type, and which ``account_type`` values
require them (enforced app-side as GL validation step 13). The base DDL added it
to ``REGISTRY_TABLES`` so fresh installs match; this migration brings already-
provisioned DBs up to parity and seeds the three default dimensions.

Pure CREATE TABLE / CREATE INDEX IF NOT EXISTS + idempotent seed (existence-guarded
INSERTs). Idempotent, dialect-aware, no rebuild / no FK-rewrite trap. Columns match
init_schema exactly.
"""
import argparse
import os
import sqlite3
import uuid

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

_DDL = [
    """CREATE TABLE IF NOT EXISTS dimension_registry (
        id                                TEXT PRIMARY KEY,
        key                               TEXT NOT NULL UNIQUE,
        label                             TEXT NOT NULL,
        data_type                         TEXT NOT NULL DEFAULT 'text'
                                          CHECK(data_type IN ('text','uuid_fk','enum')),
        referenced_table                  TEXT,
        allowed_values_json               TEXT,
        is_required_on_account_types_json TEXT,
        is_active                         INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
        created_at                        TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at                        TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_dimension_registry_active ON dimension_registry(is_active)",
]

# (key, label, data_type, referenced_table, allowed_values_json, is_required_on_account_types_json)
_SEED = [
    ("project", "Project", "uuid_fk", "project", None, None),
    ("department", "Department", "text", None, None, None),
    ("cost_center", "Cost Center", "uuid_fk", "cost_center", None, None),
]


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _seed(execute):
    """Idempotently seed the default dimensions. ``execute(sql, params)`` runs one
    statement; ``?`` placeholders work on SQLite and the Pg ``?``→``%s`` wrapper."""
    for key, label, dtype, ref_table, allowed, req in _SEED:
        existing = execute(
            "SELECT 1 FROM dimension_registry WHERE key = ?", (key,)
        ).fetchone()
        if not existing:
            execute(
                "INSERT INTO dimension_registry "
                "(id, key, label, data_type, referenced_table, allowed_values_json, "
                " is_required_on_account_types_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), key, label, dtype, ref_table, allowed, req),
            )


def _run_sqlite(path):
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='dimension_registry'"
    ).fetchone() is not None
    for stmt in _DDL:
        conn.execute(stmt)
    _seed(conn.execute)
    conn.commit()
    conn.close()
    print(f"  dimension_registry: {'already present' if existed else 'created'} (+ index, seeds).")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for stmt in _DDL:
                cur.execute(stmt)
            # Postgres uses %s; translate the ? placeholders for the seed.
            def _execute(sql, params=()):
                cur.execute(sql.replace("?", "%s"), params)
                return cur
            _seed(_execute)
        conn.commit()
        print("  Postgres: dimension_registry ensured (+ index, seeds).")
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
    parser = argparse.ArgumentParser(description="Migration 017: dimension_registry table")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 017 complete.")
