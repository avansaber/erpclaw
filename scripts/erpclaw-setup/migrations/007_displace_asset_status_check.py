"""Migration 007: Displace asset.status CHECK with a new asset_status_registry.

Wave 0 / M0 phase 4 (asset) — completes M0. Unlike phases 1-3 (which reused
existing registries), this introduces a NEW registry table. The hardcoded
status CHECK on `asset` is dropped; validity is sourced from
asset_status_registry and enforced app-side in erpclaw-assets update-asset.

1. Creates asset_status_registry (IF NOT EXISTS) and seeds the 8 states (the 5
   original + Wave-1 additions: under_construction for S3 CWIP, impaired for M7,
   cancelled for the S3 CWIP cancel path). Idempotent.
2. Drops the status CHECK from `asset` (dialect-aware):
     - SQLite: rename -> recreate WITHOUT the status CHECK (depreciation_method
       CHECK + NOT NULL DEFAULT 'draft' retained) -> intersection-copy -> drop ->
       recreate captured indexes (FK off; rows preserved verbatim).
     - PostgreSQL: ALTER TABLE asset DROP CONSTRAINT IF EXISTS asset_status_check.

Idempotent: detects an already-dropped CHECK and just (re)creates + seeds the
registry.
"""
import argparse
import os
import sqlite3
import sys

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

ASSET_STATUS_SEED = [
    ("draft", "erpclaw-assets", "Draft"),
    ("submitted", "erpclaw-assets", "Submitted"),
    ("in_use", "erpclaw-assets", "In Use"),
    ("under_construction", "erpclaw-assets", "Under Construction"),
    ("impaired", "erpclaw-assets", "Impaired"),
    ("cancelled", "erpclaw-assets", "Cancelled"),
    ("scrapped", "erpclaw-assets", "Scrapped"),
    ("sold", "erpclaw-assets", "Sold"),
]

_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS asset_status_registry (
    status       TEXT PRIMARY KEY,
    skill_name   TEXT NOT NULL,
    label        TEXT NOT NULL
)
"""

# asset WITHOUT the status CHECK. Matches init_schema.py (depreciation_method
# CHECK + NOT NULL DEFAULT 'draft' on status retained).
_ASSET_DDL_NO_CHECK = """
CREATE TABLE asset (
    id              TEXT PRIMARY KEY,
    naming_series   TEXT,
    asset_name      TEXT NOT NULL,
    asset_category_id TEXT NOT NULL REFERENCES asset_category(id) ON DELETE RESTRICT,
    item_id         TEXT REFERENCES item(id) ON DELETE RESTRICT,
    purchase_date   TEXT,
    purchase_invoice_id TEXT,
    gross_value     TEXT NOT NULL DEFAULT '0',
    salvage_value   TEXT NOT NULL DEFAULT '0',
    depreciation_method TEXT CHECK(depreciation_method IN (
                        'straight_line','written_down_value','double_declining'
                    )),
    useful_life_years INTEGER,
    depreciation_start_date TEXT,
    current_book_value TEXT NOT NULL DEFAULT '0',
    accumulated_depreciation TEXT NOT NULL DEFAULT '0',
    status          TEXT NOT NULL DEFAULT 'draft',
    location        TEXT,
    custodian_employee_id TEXT,
    warranty_expiry_date TEXT,
    company_id      TEXT NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
)
"""


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _run_sqlite(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")

    # 1. registry table + seed (idempotent, always)
    conn.execute(_REGISTRY_DDL)
    for st, skill, label in ASSET_STATUS_SEED:
        if not conn.execute(
            "SELECT 1 FROM asset_status_registry WHERE status = ?", (st,)
        ).fetchone():
            conn.execute(
                "INSERT INTO asset_status_registry (status, skill_name, label) VALUES (?, ?, ?)",
                (st, skill, label),
            )
    conn.commit()

    # 2. drop the asset.status CHECK if present
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='asset'"
    ).fetchone()
    if not row or "CHECK(status IN" not in (row[0] or ""):
        print("  asset.status CHECK already absent; asset_status_registry ensured + seeded.")
        conn.close()
        return

    old_cols = [r[1] for r in conn.execute("PRAGMA table_info(asset)")]
    index_defs = [
        r[0] for r in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='asset' AND sql IS NOT NULL"
        )
    ]
    before = conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0]

    conn.execute("PRAGMA foreign_keys=OFF")
    # legacy_alter_table=ON so the RENAME does not rewrite inbound FK references
    # (asset_movement/asset_maintenance/asset_disposal/depreciation_schedule REFERENCES
    # asset) to the dropped asset_m0_old name (see migration 003 for the rationale).
    conn.execute("PRAGMA legacy_alter_table=ON")
    try:
        conn.execute("BEGIN")
        conn.execute("ALTER TABLE asset RENAME TO asset_m0_old")
        conn.execute(_ASSET_DDL_NO_CHECK)
        new_cols = [r[1] for r in conn.execute("PRAGMA table_info(asset)")]
        dropped = [c for c in old_cols if c not in new_cols]
        if dropped:
            conn.execute("ROLLBACK")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.close()
            raise RuntimeError(
                f"Migration 007 abort: asset has columns absent from target DDL "
                f"that would be dropped: {dropped}. Update _ASSET_DDL_NO_CHECK."
            )
        common = ", ".join(c for c in new_cols if c in old_cols)
        conn.execute(f"INSERT INTO asset ({common}) SELECT {common} FROM asset_m0_old")
        conn.execute("DROP TABLE asset_m0_old")
        for ddl in index_defs:
            conn.execute(ddl)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute("PRAGMA foreign_keys=ON")

    if conn.execute("SELECT COUNT(*) FROM asset").fetchone()[0] != before:
        conn.close()
        raise RuntimeError("Migration 007 row-count mismatch on asset")
    violations = conn.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        conn.close()
        raise RuntimeError(f"Migration 007 left {len(violations)} FK violations: {violations[:5]}")
    dangling = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND sql LIKE '%_m0_old%'"
    ).fetchall()
    if dangling:
        conn.close()
        raise RuntimeError(f"Migration 007 left dangling FK refs to *_m0_old: {[r[0] for r in dangling]}")
    print(f"  asset rebuilt without status CHECK; {before} rows preserved, FK check clean; "
          f"asset_status_registry created + seeded.")
    conn.close()


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            cur.execute(_REGISTRY_DDL)
            for st, skill, label in ASSET_STATUS_SEED:
                cur.execute(
                    "INSERT INTO asset_status_registry (status, skill_name, label) "
                    "VALUES (%s, %s, %s) ON CONFLICT (status) DO NOTHING",
                    (st, skill, label),
                )
            cur.execute("ALTER TABLE asset DROP CONSTRAINT IF EXISTS asset_status_check")
        conn.commit()
        print("  Postgres: asset_status_registry created + seeded; asset.status CHECK dropped.")
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
    parser = argparse.ArgumentParser(description="Migration 007: Displace asset.status CHECK")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 007 complete.")
