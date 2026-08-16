"""Migration 027: Wave 2 S7 — item-global alternatives / substitutes.

Activates the item-global substitute relationship introduced by S7. One net-new
table, owned + written exclusively by erpclaw-inventory's db_query.py; it is
defined in the foundation schema for fresh installs (init_schema.py
INVENTORY_TABLES) and added to existing DBs here, mirroring the M2 bank-statement
precedent (020) and the M5 putaway/pick/reservation precedent (025).

  - item_alternative: a directional (item_id -> alternative_item_id) substitute
    relationship. priority ASC = preferred (lower wins). conversion_factor is a
    Decimal-as-text qty multiplier. UNIQUE(item_id, alternative_item_id) blocks a
    duplicate pair; (a,b) and (b,a) are BOTH valid distinct rows (directional).
    The CHECK(item_id != alternative_item_id) forbids self-substitution at the
    schema layer. Soft-deleted via is_active=0.

Manufacturing's `add-bom-substitute` performs a permitted cross-module READ of
this table (any module may READ any table; only inventory WRITES it). The table
is read, never written, by manufacturing.

Pure CREATE TABLE / CREATE INDEX IF NOT EXISTS. Idempotent, dialect-aware, no
rebuild / no FK-rewrite trap. Columns match init_schema exactly. SIM-0-validated
(wave rehearsal harness, _DDL_027_ITEM_ALTERNATIVE):
creation, FK targets, idempotency, cross-dialect shape all PASS.
"""
import argparse
import os
import sqlite3

# M102: adds tables / columns / indexes only — nothing a row held before this
# run is different afterwards.
MIGRATION_DATA_CLASS = "none"

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

_DDL = [
    """CREATE TABLE IF NOT EXISTS item_alternative (
        id                  TEXT PRIMARY KEY,
        item_id             TEXT NOT NULL REFERENCES item(id) ON DELETE RESTRICT,
        alternative_item_id TEXT NOT NULL REFERENCES item(id) ON DELETE RESTRICT,
        priority            INTEGER NOT NULL DEFAULT 100,
        conversion_factor   TEXT NOT NULL DEFAULT '1',
        notes               TEXT,
        is_active           INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
        created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(item_id, alternative_item_id),
        CHECK(item_id != alternative_item_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_item_alt_item ON item_alternative(item_id, is_active, priority)",
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
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='item_alternative'"
    ).fetchone() is not None
    for stmt in _DDL:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    print(f"  item_alternative: {'already present' if existed else 'created'} (+ index).")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for stmt in _DDL:
                cur.execute(stmt)
        conn.commit()
        print("  Postgres: item_alternative ensured (+ index).")
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
    parser = argparse.ArgumentParser(description="Migration 027: Wave 2 S7 item-global alternatives")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 027 complete.")
