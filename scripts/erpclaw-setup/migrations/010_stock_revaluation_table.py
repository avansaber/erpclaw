"""Migration 010: Create the stock_revaluation table (BUG-006 fix).

erpclaw-inventory `revalue-stock` / `list-stock-revaluations` /
`get-stock-revaluation` / `cancel-stock-revaluation` write and read this table,
but it was never created in any schema — the actions crashed with "no such
table". This adds it to existing DBs. Columns match init_schema exactly.

Pure CREATE TABLE / CREATE INDEX IF NOT EXISTS — idempotent, dialect-aware,
no rebuild / no FK-rewrite trap.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

_DDL = [
    """CREATE TABLE IF NOT EXISTS stock_revaluation (
        id                TEXT PRIMARY KEY,
        naming_series     TEXT,
        company_id        TEXT NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        item_id           TEXT NOT NULL REFERENCES item(id) ON DELETE RESTRICT,
        warehouse_id      TEXT NOT NULL REFERENCES warehouse(id) ON DELETE RESTRICT,
        posting_date      TEXT NOT NULL,
        current_qty       TEXT,
        old_rate          TEXT,
        new_rate          TEXT,
        adjustment_amount TEXT,
        reason            TEXT,
        status            TEXT NOT NULL DEFAULT 'submitted'
                          CHECK(status IN ('submitted','cancelled')),
        created_at        TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at        TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_stock_reval_item ON stock_revaluation(item_id, warehouse_id)",
    "CREATE INDEX IF NOT EXISTS idx_stock_reval_company ON stock_revaluation(company_id)",
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
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='stock_revaluation'"
    ).fetchone() is not None
    for stmt in _DDL:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    print(f"  stock_revaluation: {'already present' if existed else 'created'} (+ indexes).")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for stmt in _DDL:
                cur.execute(stmt)
        conn.commit()
        print("  Postgres: stock_revaluation ensured (+ indexes).")
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
    parser = argparse.ArgumentParser(description="Migration 010: stock_revaluation table")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 010 complete.")
