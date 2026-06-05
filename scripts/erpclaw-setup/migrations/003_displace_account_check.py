"""Migration 003: Displace the hardcoded account_type CHECK with the registry.

Wave 0 / M0 phase 1 (account). Migration 001 created + seeded
account_type_registry but left the hardcoded CHECK on account.account_type in
place, so the registry was inert and runtime-registered types (e.g. 'trust')
were rejected. This migration:

1. Drops the account_type CHECK from the `account` table (dialect-aware):
     - SQLite: rename -> recreate WITHOUT the CHECK -> copy -> drop -> reindex
       (SQLite cannot ALTER TABLE DROP a CHECK). FK enforcement is OFF during
       the rebuild; all account ids are preserved so the 19 inbound FKs stay
       valid. root_type CHECK + boolean CHECKs + UNIQUE are retained.
     - PostgreSQL: ALTER TABLE account DROP CONSTRAINT IF EXISTS
       account_account_type_check (the name Postgres assigns the inline CHECK).
2. Seeds account_type_registry with the canonical 24 types (the 21 from
   migration 001 + the 3 Wave-1 prerequisites: capital_work_in_progress,
   goodwill, revaluation_reserve). Idempotent.

After this, account_type validity is sourced from account_type_registry and
enforced app-side in erpclaw-gl add-account. Fresh installs get the same end
state from init_schema.py (CHECK already removed there + seeded in init_db()).

Idempotent: safe to run multiple times. Detects whether the CHECK is already
gone and skips the rebuild if so.

Usage:
    python3 003_displace_account_check.py [--db-path PATH]
"""
import argparse
import os
import sqlite3
import sys

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

# Self-contained snapshot of the canonical seed (migrations are frozen; kept in
# sync with init_schema.ACCOUNT_TYPE_REGISTRY_SEED at authoring time).
ACCOUNT_TYPE_SEED = [
    ("bank", "erpclaw-gl", "Bank"),
    ("cash", "erpclaw-gl", "Cash"),
    ("receivable", "erpclaw-selling", "Receivable"),
    ("payable", "erpclaw-buying", "Payable"),
    ("stock", "erpclaw-inventory", "Stock"),
    ("fixed_asset", "erpclaw-assets", "Fixed Asset"),
    ("accumulated_depreciation", "erpclaw-assets", "Accumulated Depreciation"),
    ("cost_of_goods_sold", "erpclaw-selling", "Cost of Goods Sold"),
    ("tax", "erpclaw-tax", "Tax"),
    ("equity", "erpclaw-gl", "Equity"),
    ("revenue", "erpclaw-selling", "Revenue"),
    ("expense", "erpclaw-gl", "Expense"),
    ("stock_received_not_billed", "erpclaw-buying", "Stock Received Not Billed"),
    ("stock_adjustment", "erpclaw-inventory", "Stock Adjustment"),
    ("rounding", "erpclaw-gl", "Rounding"),
    ("exchange_gain_loss", "erpclaw-gl", "Exchange Gain/Loss"),
    ("depreciation", "erpclaw-assets", "Depreciation"),
    ("payroll_payable", "erpclaw-payroll", "Payroll Payable"),
    ("temporary", "erpclaw-gl", "Temporary"),
    ("asset_received_not_billed", "erpclaw-assets", "Asset Received Not Billed"),
    ("trust", "erpclaw-gl", "Trust"),
    ("capital_work_in_progress", "erpclaw-assets", "Capital Work in Progress"),
    ("goodwill", "erpclaw-accounting-adv", "Goodwill"),
    ("revaluation_reserve", "erpclaw-assets", "Revaluation Reserve"),
]

# The `account` table WITHOUT the account_type CHECK. Must match init_schema.py.
_ACCOUNT_DDL_NO_CHECK = """
CREATE TABLE account (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    account_number  TEXT,
    parent_id       TEXT REFERENCES account(id) ON DELETE RESTRICT,
    root_type       TEXT NOT NULL CHECK(root_type IN ('asset','liability','equity','income','expense')),
    account_type    TEXT,
    currency        TEXT NOT NULL DEFAULT 'USD',
    is_group        INTEGER NOT NULL DEFAULT 0 CHECK(is_group IN (0,1)),
    is_frozen       INTEGER NOT NULL DEFAULT 0 CHECK(is_frozen IN (0,1)),
    disabled        INTEGER NOT NULL DEFAULT 0 CHECK(disabled IN (0,1)),
    balance_direction TEXT NOT NULL DEFAULT 'debit_normal'
                    CHECK(balance_direction IN ('debit_normal','credit_normal')),
    balance_must_be TEXT CHECK(balance_must_be IN ('debit','credit')),
    company_id      TEXT NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
    depth           INTEGER NOT NULL DEFAULT 0,
    lft             INTEGER,
    rgt             INTEGER,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(account_number, company_id)
)
"""

_ACCOUNT_COLS = (
    "id, name, account_number, parent_id, root_type, account_type, currency, "
    "is_group, is_frozen, disabled, balance_direction, balance_must_be, "
    "company_id, depth, lft, rgt, created_at, updated_at"
)

_ACCOUNT_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_account_company ON account(company_id)",
    "CREATE INDEX IF NOT EXISTS idx_account_parent ON account(parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_account_root_type ON account(root_type)",
    "CREATE INDEX IF NOT EXISTS idx_account_type ON account(account_type)",
]


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _sqlite_check_present(conn):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='account'"
    ).fetchone()
    return bool(row) and "account_type IN" in (row[0] or "")


def _seed_account_types(conn):
    for at, skill, label in ACCOUNT_TYPE_SEED:
        existing = conn.execute(
            "SELECT 1 FROM account_type_registry WHERE account_type = ?", (at,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO account_type_registry (account_type, skill_name, label) "
                "VALUES (?, ?, ?)",
                (at, skill, label),
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
        # CHECK already gone (re-run or fresh DB from new init_schema); just seed.
        _seed_account_types(conn)
        conn.commit()
        print("  account_type CHECK already absent; registry re-seeded (idempotent).")
        conn.close()
        return

    conn.execute("PRAGMA foreign_keys=OFF")  # required during table rebuild
    # CRITICAL: modern SQLite's ALTER TABLE RENAME rewrites inbound FK references in
    # OTHER tables (e.g. gl_entry.account_id REFERENCES account) to the new name. Without
    # legacy mode, "RENAME account -> account_m0_old" would redirect ~18 child tables to
    # account_m0_old, which we then drop -> dangling FK refs. legacy_alter_table=ON keeps
    # child references pointing at "account".
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE account RENAME TO account_m0_old")
        conn.execute(_ACCOUNT_DDL_NO_CHECK)
        conn.execute(
            f"INSERT INTO account ({_ACCOUNT_COLS}) SELECT {_ACCOUNT_COLS} FROM account_m0_old"
        )
        conn.execute("DROP TABLE account_m0_old")
        for idx in _ACCOUNT_INDEXES:
            conn.execute(idx)
        _seed_account_types(conn)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")

    # FK integrity sanity after rebuild
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        conn.close()
        raise RuntimeError(f"Migration 003 left {len(violations)} FK violations: {violations[:5]}")
    # Guard against the RENAME-rewrites-FK trap: no table may reference a dropped
    # *_m0_old table. (foreign_key_check does NOT catch references to a missing table.)
    dangling = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%_m0_old%'"
    ).fetchall()
    if dangling:
        conn.close()
        raise RuntimeError(f"Migration 003 left dangling FK refs to *_m0_old: {[r[0] for r in dangling]}")
    n = conn.execute("SELECT COUNT(*) FROM account").fetchone()[0]
    print(f"  account rebuilt without account_type CHECK; {n} rows preserved, FK check clean.")
    conn.close()


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE account DROP CONSTRAINT IF EXISTS account_account_type_check"
            )
            for at, skill, label in ACCOUNT_TYPE_SEED:
                cur.execute(
                    "INSERT INTO account_type_registry (account_type, skill_name, label) "
                    "VALUES (%s, %s, %s) ON CONFLICT (account_type) DO NOTHING",
                    (at, skill, label),
                )
        conn.commit()
        print("  Postgres: account_type CHECK dropped (IF EXISTS); registry seeded.")
    finally:
        conn.close()


def run_migration(db_path=None):
    dialect = _get_dialect()
    if dialect == "postgresql":
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
    parser = argparse.ArgumentParser(description="Migration 003: Displace account_type CHECK")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 003 complete.")
