"""Migration 005: Displace stock_ledger_entry's voucher_type CHECK with the registry.

Wave 0 / M0 phase 3a (stock_ledger_entry). The hardcoded voucher_type CHECK on
stock_ledger_entry is dropped; validity is sourced from voucher_type_registry
(target_table='stock_ledger_entry') and enforced app-side in
stock_posting.insert_sle_entries. Consistent with M0 phases 1-2.

1. Drops the voucher_type CHECK from `stock_ledger_entry` (dialect-aware):
     - SQLite: rename -> recreate WITHOUT the CHECK -> intersection-copy ->
       drop -> recreate captured indexes (FK off; rows preserved verbatim; SLE
       is immutable). is_cancelled CHECK retained.
     - PostgreSQL: ALTER TABLE ... DROP CONSTRAINT IF EXISTS
       stock_ledger_entry_voucher_type_check.
2. Seeds the 10 stock_ledger_entry voucher types (idempotent).

Idempotent: detects an already-dropped CHECK and just re-seeds.
"""
import argparse
import os
import sqlite3
import sys

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

SLE_VOUCHER_SEED = [
    ("stock_entry", "erpclaw-inventory", "Stock Entry"),
    ("purchase_receipt", "erpclaw-buying", "Purchase Receipt"),
    ("delivery_note", "erpclaw-selling", "Delivery Note"),
    ("stock_reconciliation", "erpclaw-inventory", "Stock Reconciliation"),
    ("work_order", "erpclaw-manufacturing", "Work Order"),
    ("sales_invoice", "erpclaw-selling", "Sales Invoice"),
    ("credit_note", "erpclaw-selling", "Credit Note"),
    ("purchase_invoice", "erpclaw-buying", "Purchase Invoice"),
    ("debit_note", "erpclaw-buying", "Debit Note"),
    ("stock_revaluation", "erpclaw-inventory", "Stock Revaluation"),
]

# stock_ledger_entry WITHOUT the voucher_type CHECK. Matches init_schema.py.
_SLE_DDL_NO_CHECK = """
CREATE TABLE stock_ledger_entry (
    id              TEXT PRIMARY KEY,
    posting_date    TEXT NOT NULL,
    posting_time    TEXT,
    item_id         TEXT NOT NULL REFERENCES item(id) ON DELETE RESTRICT,
    warehouse_id    TEXT NOT NULL REFERENCES warehouse(id) ON DELETE RESTRICT,
    actual_qty      TEXT NOT NULL DEFAULT '0',
    qty_after_transaction TEXT NOT NULL DEFAULT '0',
    valuation_rate  TEXT NOT NULL DEFAULT '0',
    stock_value     TEXT NOT NULL DEFAULT '0',
    stock_value_difference TEXT NOT NULL DEFAULT '0',
    voucher_type    TEXT NOT NULL,
    voucher_id      TEXT NOT NULL,
    batch_id        TEXT,
    serial_number   TEXT,
    incoming_rate   TEXT NOT NULL DEFAULT '0',
    is_cancelled    INTEGER NOT NULL DEFAULT 0 CHECK(is_cancelled IN (0,1)),
    fiscal_year     TEXT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _seed(conn):
    for vt, skill, label in SLE_VOUCHER_SEED:
        if not conn.execute(
            "SELECT 1 FROM voucher_type_registry WHERE voucher_type = ? AND target_table = 'stock_ledger_entry'",
            (vt,),
        ).fetchone():
            conn.execute(
                "INSERT INTO voucher_type_registry (voucher_type, skill_name, label, target_table) "
                "VALUES (?, ?, ?, 'stock_ledger_entry')",
                (vt, skill, label),
            )


def _run_sqlite(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='stock_ledger_entry'"
    ).fetchone()
    if not row or "voucher_type IN" not in (row[0] or ""):
        _seed(conn)
        conn.commit()
        print("  stock_ledger_entry voucher_type CHECK already absent; registry re-seeded.")
        conn.close()
        return

    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(stock_ledger_entry)")]
    index_defs = [
        r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='stock_ledger_entry' "
            "AND sql IS NOT NULL"
        )
    ]
    row_count_before = conn.execute("SELECT COUNT(*) FROM stock_ledger_entry").fetchone()[0]

    conn.execute("PRAGMA foreign_keys=OFF")
    # legacy_alter_table=ON so the RENAME does not rewrite inbound FK references to the
    # dropped *_m0_old name (see migration 003 for the full rationale).
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE stock_ledger_entry RENAME TO sle_m0_old")
        conn.execute(_SLE_DDL_NO_CHECK)
        new_cols = [r[1] for r in conn.execute("PRAGMA table_info(stock_ledger_entry)")]
        dropped = [c for c in old_cols if c not in new_cols]
        if dropped:
            conn.execute("ROLLBACK")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.close()
            raise RuntimeError(
                f"Migration 005 abort: stock_ledger_entry has columns absent from the "
                f"target DDL that would be dropped: {dropped}. Update _SLE_DDL_NO_CHECK."
            )
        common = ", ".join(c for c in new_cols if c in old_cols)
        conn.execute(f"INSERT INTO stock_ledger_entry ({common}) SELECT {common} FROM sle_m0_old")
        conn.execute("DROP TABLE sle_m0_old")
        for ddl in index_defs:
            conn.execute(ddl)
        _seed(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")

    if conn.execute("SELECT COUNT(*) FROM stock_ledger_entry").fetchone()[0] != row_count_before:
        conn.close()
        raise RuntimeError("Migration 005 row-count mismatch")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        conn.close()
        raise RuntimeError(f"Migration 005 left {len(violations)} FK violations: {violations[:5]}")
    dangling = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%_m0_old%'"
    ).fetchall()
    if dangling:
        conn.close()
        raise RuntimeError(f"Migration 005 left dangling FK refs to *_m0_old: {[r[0] for r in dangling]}")
    print(f"  stock_ledger_entry rebuilt without voucher_type CHECK; "
          f"{row_count_before} rows preserved, FK check clean.")
    conn.close()


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE stock_ledger_entry DROP CONSTRAINT IF EXISTS "
                "stock_ledger_entry_voucher_type_check"
            )
            for vt, skill, label in SLE_VOUCHER_SEED:
                cur.execute(
                    "INSERT INTO voucher_type_registry (voucher_type, skill_name, label, target_table) "
                    "VALUES (%s, %s, %s, 'stock_ledger_entry') ON CONFLICT (voucher_type, target_table) DO NOTHING",
                    (vt, skill, label),
                )
        conn.commit()
        print("  Postgres: stock_ledger_entry voucher_type CHECK dropped; registry seeded.")
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
    parser = argparse.ArgumentParser(description="Migration 005: Displace stock_ledger_entry voucher_type CHECK")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 005 complete.")
