"""Migration 026: Wave 2 S5 — subcontracting charge + completion-timestamp columns.

Completes the subcontracting lifecycle (draft → submitted → partially_received →
completed) introduced by S5. Two net-new NULLABLE TEXT columns on the existing
subcontracting_order table (owned + written exclusively by erpclaw-manufacturing).
Both are defined in the foundation schema for fresh installs (init_schema.py) and
added to existing DBs here, mirroring the additive-column precedent (023).

  - subcontract_charge_rate: Decimal-as-text per-unit subcontracting fee. The FG
    cost on receipt is raw-material cost + (subcontract_charge_rate × received_qty).
    Nullable: an order created before this column existed simply has no charge until
    receive-subcontracted-items supplies a rate.
  - final_received_at: completion timestamp, set when received_qty reaches qty and
    the order flips to 'completed'. Nullable until completion.

Plain ADD COLUMN of plain nullable TEXT columns (no DEFAULT, no rebuild, no
FK-rewrite). Idempotent (SQLite guarded by _sqlite_has_column; Postgres
ADD COLUMN IF NOT EXISTS). Dialect-aware. SIM-0-validated
(wave rehearsal harness): creation, idempotency, and
cross-dialect shape all PASS — the columns are pure additive TEXT, so a
foundation-only install carries them simply unpopulated.
"""
import argparse
import os
import sqlite3

# M102: adds tables / columns / indexes only — nothing a row held before this
# run is different afterwards.
MIGRATION_DATA_CLASS = "none"

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

# (table, column). Nullable, no DEFAULT, plain TEXT (Decimal-as-text fee /
# completion timestamp). See module docstring.
_COLUMNS = [
    ("subcontracting_order", "subcontract_charge_rate"),
    ("subcontracting_order", "final_received_at"),
]


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _sqlite_has_table(conn, table):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


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
    for table, column in _COLUMNS:
        if _sqlite_has_table(conn, table) and not _sqlite_has_column(conn, table, column):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
            added.append(f"{table}.{column}")
    conn.commit()
    conn.close()
    print(f"  Subcontracting charge columns added: {', '.join(added) if added else '(none — already present)'}")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for table, column in _COLUMNS:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} TEXT")
        conn.commit()
        print("  Postgres: subcontracting charge columns ensured.")
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
    parser = argparse.ArgumentParser(description="Migration 026: Wave 2 S5 subcontracting charge columns")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 026 complete.")
