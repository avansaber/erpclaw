"""Migration 004: Displace gl_entry's voucher_type + party_type CHECKs with registries.

Wave 0 / M0 phase 2 (gl_entry). The hardcoded CHECKs on gl_entry.voucher_type
and gl_entry.party_type are dropped; validity is sourced from
voucher_type_registry (target_table='gl_entry') and party_type_registry and
enforced app-side in gl_posting.insert_gl_entries. This makes the types
extensible at runtime, consistent with M0 phase 1 (account).

1. Drops both CHECKs from `gl_entry` (dialect-aware):
     - SQLite: rename -> recreate WITHOUT the two CHECKs -> intersection-copy ->
       drop -> recreate indexes. FK enforcement OFF during the rebuild; all
       gl_entry rows (incl. gl_checksum chain + dimensions_json) preserved
       verbatim, so the immutable ledger and its tamper-evidence chain are
       intact. The is_cancelled CHECK is retained.
     - PostgreSQL: ALTER TABLE gl_entry DROP CONSTRAINT IF EXISTS
       gl_entry_voucher_type_check / gl_entry_party_type_check.
2. Seeds voucher_type_registry (gl_entry/stock_ledger_entry/payment_allocation)
   + party_type_registry. Idempotent.

The SQLite rebuild is column-aware: it copies the intersection of old and new
columns and recreates whatever indexes existed (captured before the rename), so
it is robust to column/index drift (e.g. dimensions_json added by migration 001;
also added to the base init_schema DDL as of M0 phase 2). Migration order
(001 before 004) guarantees dimensions_json exists when this runs.

Idempotent: detects an already-dropped CHECK and just re-seeds.

Usage:
    python3 004_displace_gl_entry_checks.py [--db-path PATH]
"""
import argparse
import os
import sqlite3
import sys

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

# Self-contained snapshot of the canonical seeds (kept in sync with
# init_schema.VOUCHER_TYPE_REGISTRY_SEED / PARTY_TYPE_REGISTRY_SEED).
VOUCHER_TYPE_SEED = [
    ("journal_entry", "erpclaw-journals", "Journal Entry", "gl_entry"),
    ("sales_invoice", "erpclaw-selling", "Sales Invoice", "gl_entry"),
    ("purchase_invoice", "erpclaw-buying", "Purchase Invoice", "gl_entry"),
    ("payment_entry", "erpclaw-payments", "Payment Entry", "gl_entry"),
    ("stock_entry", "erpclaw-inventory", "Stock Entry", "gl_entry"),
    ("depreciation_entry", "erpclaw-assets", "Depreciation Entry", "gl_entry"),
    ("payroll_entry", "erpclaw-payroll", "Payroll Entry", "gl_entry"),
    ("period_closing", "erpclaw-gl", "Period Closing", "gl_entry"),
    ("expense_claim", "erpclaw-hr", "Expense Claim", "gl_entry"),
    ("asset_disposal", "erpclaw-assets", "Asset Disposal", "gl_entry"),
    ("stock_reconciliation", "erpclaw-inventory", "Stock Reconciliation", "gl_entry"),
    ("purchase_receipt", "erpclaw-buying", "Purchase Receipt", "gl_entry"),
    ("delivery_note", "erpclaw-selling", "Delivery Note", "gl_entry"),
    ("credit_note", "erpclaw-selling", "Credit Note", "gl_entry"),
    ("debit_note", "erpclaw-buying", "Debit Note", "gl_entry"),
    ("work_order", "erpclaw-manufacturing", "Work Order", "gl_entry"),
    ("exchange_rate_revaluation", "erpclaw-gl", "Exchange Rate Revaluation", "gl_entry"),
    ("stock_revaluation", "erpclaw-inventory", "Stock Revaluation", "gl_entry"),
    ("elimination_entry", "erpclaw-gl", "Elimination Entry", "gl_entry"),
    ("stock_entry", "erpclaw-inventory", "Stock Entry", "stock_ledger_entry"),
    ("purchase_receipt", "erpclaw-buying", "Purchase Receipt", "stock_ledger_entry"),
    ("delivery_note", "erpclaw-selling", "Delivery Note", "stock_ledger_entry"),
    ("stock_reconciliation", "erpclaw-inventory", "Stock Reconciliation", "stock_ledger_entry"),
    ("work_order", "erpclaw-manufacturing", "Work Order", "stock_ledger_entry"),
    ("sales_invoice", "erpclaw-selling", "Sales Invoice", "stock_ledger_entry"),
    ("credit_note", "erpclaw-selling", "Credit Note", "stock_ledger_entry"),
    ("purchase_invoice", "erpclaw-buying", "Purchase Invoice", "stock_ledger_entry"),
    ("debit_note", "erpclaw-buying", "Debit Note", "stock_ledger_entry"),
    ("stock_revaluation", "erpclaw-inventory", "Stock Revaluation", "stock_ledger_entry"),
    ("sales_invoice", "erpclaw-selling", "Sales Invoice", "payment_allocation"),
    ("purchase_invoice", "erpclaw-buying", "Purchase Invoice", "payment_allocation"),
    ("credit_note", "erpclaw-selling", "Credit Note", "payment_allocation"),
    ("debit_note", "erpclaw-buying", "Debit Note", "payment_allocation"),
]

PARTY_TYPE_SEED = [
    ("customer", "erpclaw-selling", "Customer"),
    ("supplier", "erpclaw-buying", "Supplier"),
    ("employee", "erpclaw-hr", "Employee"),
]

# gl_entry WITHOUT the voucher_type / party_type CHECKs. Matches init_schema.py
# (is_cancelled CHECK retained; dimensions_json in the base DDL).
_GL_ENTRY_DDL_NO_CHECK = """
CREATE TABLE gl_entry (
    id              TEXT PRIMARY KEY,
    posting_date    TEXT NOT NULL,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    account_id      TEXT NOT NULL REFERENCES account(id) ON DELETE RESTRICT,
    party_type      TEXT,
    party_id        TEXT,
    debit           TEXT NOT NULL DEFAULT '0',
    credit          TEXT NOT NULL DEFAULT '0',
    currency        TEXT NOT NULL DEFAULT 'USD',
    debit_base      TEXT NOT NULL DEFAULT '0',
    credit_base     TEXT NOT NULL DEFAULT '0',
    exchange_rate   TEXT NOT NULL DEFAULT '1',
    voucher_type    TEXT NOT NULL,
    voucher_id      TEXT NOT NULL,
    entry_set       TEXT NOT NULL DEFAULT 'primary',
    cost_center_id  TEXT REFERENCES cost_center(id) ON DELETE RESTRICT,
    project_id      TEXT,
    remarks         TEXT,
    fiscal_year     TEXT,
    is_cancelled    INTEGER NOT NULL DEFAULT 0 CHECK(is_cancelled IN (0,1)),
    cancelled_by    TEXT,
    sequence        INTEGER,
    gl_checksum     TEXT,
    dimensions_json TEXT NOT NULL DEFAULT '{}'
)
"""


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _sqlite_check_present(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='gl_entry'"
    ).fetchone()
    sql = (row[0] or "") if row else ""
    return "voucher_type IN" in sql or "party_type IN" in sql


def _seed(conn):
    for vt, skill, label, target in VOUCHER_TYPE_SEED:
        if not conn.execute(
            "SELECT 1 FROM voucher_type_registry WHERE voucher_type = ? AND target_table = ?",
            (vt, target),
        ).fetchone():
            conn.execute(
                "INSERT INTO voucher_type_registry (voucher_type, skill_name, label, target_table) "
                "VALUES (?, ?, ?, ?)",
                (vt, skill, label, target),
            )
    for pt, skill, label in PARTY_TYPE_SEED:
        if not conn.execute(
            "SELECT 1 FROM party_type_registry WHERE party_type = ?", (pt,)
        ).fetchone():
            conn.execute(
                "INSERT INTO party_type_registry (party_type, skill_name, label) VALUES (?, ?, ?)",
                (pt, skill, label),
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

    if not _sqlite_check_present(conn):
        _seed(conn)
        conn.commit()
        print("  gl_entry voucher_type/party_type CHECKs already absent; registries re-seeded.")
        conn.close()
        return

    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(gl_entry)")]
    # Capture index defs BEFORE the rename (their stored SQL says "ON gl_entry").
    index_defs = [
        r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='gl_entry' "
            "AND sql IS NOT NULL"
        )
    ]
    row_count_before = conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0]

    conn.execute("PRAGMA foreign_keys=OFF")
    # legacy_alter_table=ON so the RENAME does not rewrite any inbound FK references in
    # other tables to the dropped *_m0_old name (see migration 003 for the full rationale).
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE gl_entry RENAME TO gl_entry_m0_old")
        conn.execute(_GL_ENTRY_DDL_NO_CHECK)
        new_cols = [r[1] for r in conn.execute("PRAGMA table_info(gl_entry)")]
        common = [c for c in new_cols if c in old_cols]
        dropped = [c for c in old_cols if c not in new_cols]
        if dropped:
            # Unexpected extra columns on the old table — refuse to silently lose data.
            conn.execute("ROLLBACK")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.close()
            raise RuntimeError(
                f"Migration 004 abort: gl_entry has columns absent from the target DDL "
                f"that would be dropped: {dropped}. Update _GL_ENTRY_DDL_NO_CHECK first."
            )
        collist = ", ".join(common)
        conn.execute(f"INSERT INTO gl_entry ({collist}) SELECT {collist} FROM gl_entry_m0_old")
        conn.execute("DROP TABLE gl_entry_m0_old")
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

    row_count_after = conn.execute("SELECT COUNT(*) FROM gl_entry").fetchone()[0]
    if row_count_after != row_count_before:
        conn.close()
        raise RuntimeError(
            f"Migration 004 row-count mismatch: before={row_count_before} after={row_count_after}"
        )
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        conn.close()
        raise RuntimeError(f"Migration 004 left {len(violations)} FK violations: {violations[:5]}")
    dangling = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%_m0_old%'"
    ).fetchall()
    if dangling:
        conn.close()
        raise RuntimeError(f"Migration 004 left dangling FK refs to *_m0_old: {[r[0] for r in dangling]}")
    print(f"  gl_entry rebuilt without voucher_type/party_type CHECKs; "
          f"{row_count_after} rows preserved, FK check clean.")
    conn.close()


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE gl_entry DROP CONSTRAINT IF EXISTS gl_entry_voucher_type_check")
            cur.execute("ALTER TABLE gl_entry DROP CONSTRAINT IF EXISTS gl_entry_party_type_check")
            for vt, skill, label, target in VOUCHER_TYPE_SEED:
                cur.execute(
                    "INSERT INTO voucher_type_registry (voucher_type, skill_name, label, target_table) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (voucher_type, target_table) DO NOTHING",
                    (vt, skill, label, target),
                )
            for pt, skill, label in PARTY_TYPE_SEED:
                cur.execute(
                    "INSERT INTO party_type_registry (party_type, skill_name, label) "
                    "VALUES (%s, %s, %s) ON CONFLICT (party_type) DO NOTHING",
                    (pt, skill, label),
                )
        conn.commit()
        print("  Postgres: gl_entry voucher_type/party_type CHECKs dropped; registries seeded.")
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
    parser = argparse.ArgumentParser(description="Migration 004: Displace gl_entry CHECKs")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 004 complete.")
