"""Migration 022: S3 CWIP invoice/JE hooks (AVA-43) — purchase_invoice.cwip_asset_id
and journal_entry.cwip_asset_id columns.

The S3 core (migration 021) shipped the cwip_cost_accumulation table + 5 actions.
This migration adds the remaining acceptance criterion: the --cwip-asset-id flag on
create-purchase-invoice / add-journal-entry. The flag is captured at create and
consumed at submit (where GL posts), so it must persist on the document — one
nullable column per table carries it.

Both columns are plain TEXT, no FK — mirroring asset.cwip_project_id (migration
021): the referenced asset is validated app-side (must be under_construction) by
the submit hook, not by a DB constraint. Columns match init_schema exactly.

Pure column-presence-guarded ADD COLUMN. Idempotent, dialect-aware, no rebuild /
no FK-rewrite trap. Pairs with migration 021 in the S3 sequence.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

# (table, column) — each added as plain nullable TEXT (validated app-side).
_ADDITIONS = [
    ("purchase_invoice", "cwip_asset_id"),
    ("journal_entry", "cwip_asset_id"),
]
# Fully-literal SQL per table/column (no interpolation of untrusted input — the
# table/column names are fixed constants).
_SQLITE_ADD = {
    ("purchase_invoice", "cwip_asset_id"):
        "ALTER TABLE purchase_invoice ADD COLUMN cwip_asset_id TEXT",
    ("journal_entry", "cwip_asset_id"):
        "ALTER TABLE journal_entry ADD COLUMN cwip_asset_id TEXT",
}
_PG_ADD = {
    ("purchase_invoice", "cwip_asset_id"):
        "ALTER TABLE purchase_invoice ADD COLUMN IF NOT EXISTS cwip_asset_id TEXT",
    ("journal_entry", "cwip_asset_id"):
        "ALTER TABLE journal_entry ADD COLUMN IF NOT EXISTS cwip_asset_id TEXT",
}


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
    for table, column in _ADDITIONS:
        if not _sqlite_has_column(conn, table, column):
            conn.execute(_SQLITE_ADD[(table, column)])
            print(f"  {table}.{column}: added.")
        else:
            print(f"  {table}.{column}: already present.")
    conn.commit()
    conn.close()


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for table, column in _ADDITIONS:
                cur.execute(_PG_ADD[(table, column)])
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
    parser = argparse.ArgumentParser(description="Migration 022: S3 CWIP invoice/JE hooks")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 022 complete.")
