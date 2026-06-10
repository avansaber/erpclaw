"""Migration 021: S3 CWIP — cwip_cost_accumulation table + cwip_capitalization
voucher type + asset.cwip_project_id column.

Activates the construction-in-progress workflow. The CWIP guard already lives in
gl_posting.py and the capital_work_in_progress account_type / under_construction +
cancelled asset statuses were seeded by M0 (migrations 003 / 007 / 018); this
migration adds the remaining S3 objects:

  - cwip_cost_accumulation: one immutable row per cost accumulated against an
    under-construction asset. Submit-only (no updated_at) — cancel = reverse via
    a mirror GL + status='reversed', per the coding rules.
  - cwip_capitalization gl_entry voucher type (the accumulate-cwip-cost leg
    DR CWIP / CR source posts under it; the JE guard only blocks journal_entry,
    so this dedicated type reaches the CWIP account legitimately).
  - asset.cwip_project_id: nullable, plain TEXT (mirrors gl_entry.project_id —
    no FK, validated app-side). Carries the project an under-construction asset
    belongs to so every accumulation stamps gl_entry.project_id for per-project
    CWIP roll-up (Wave-1 open question #7).

Pure CREATE TABLE / CREATE INDEX IF NOT EXISTS + a column-presence-guarded
ADD COLUMN + an existence-guarded seed INSERT. Idempotent, dialect-aware, no
rebuild / no FK-rewrite trap. Columns match init_schema exactly. Follows S3 in
the M7→S3 sequence (shares the erpclaw-assets module); pairs with migration 018.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

_DDL = [
    """CREATE TABLE IF NOT EXISTS cwip_cost_accumulation (
        id                  TEXT PRIMARY KEY,
        asset_id            TEXT NOT NULL REFERENCES asset(id) ON DELETE RESTRICT,
        source_voucher_type TEXT NOT NULL,
        source_voucher_id   TEXT,
        accumulated_amount  TEXT NOT NULL DEFAULT '0',
        gl_entry_id         TEXT,
        status              TEXT NOT NULL DEFAULT 'submitted'
                            CHECK(status IN ('submitted','reversed')),
        accumulated_at      TEXT NOT NULL,
        notes               TEXT,
        created_at          TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cwip_accum_asset ON cwip_cost_accumulation(asset_id)",
    "CREATE INDEX IF NOT EXISTS idx_cwip_accum_status ON cwip_cost_accumulation(status)",
]

# (voucher_type, skill_name, label, target_table) — gl_entry.
_VOUCHER_SEED = [
    ("cwip_capitalization", "erpclaw-assets", "CWIP Capitalization", "gl_entry"),
]

_ASSET_TABLE = "asset"
_ASSET_COLUMN = "cwip_project_id"
# Fully-literal SQL (table + column are fixed constants — no interpolation).
_SQLITE_ADD = "ALTER TABLE asset ADD COLUMN cwip_project_id TEXT"
_PG_ADD = "ALTER TABLE asset ADD COLUMN IF NOT EXISTS cwip_project_id TEXT"


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _seed(execute):
    """Idempotently seed the cwip_capitalization voucher type. ``execute(sql,
    params)`` runs one statement; ``?`` placeholders work on SQLite and the
    Pg ``?``→``%s`` wrapper."""
    for vt, skill, label, target in _VOUCHER_SEED:
        existing = execute(
            "SELECT 1 FROM voucher_type_registry WHERE voucher_type = ? AND target_table = ?",
            (vt, target),
        ).fetchone()
        if not existing:
            execute(
                "INSERT INTO voucher_type_registry (voucher_type, skill_name, label, target_table) "
                "VALUES (?, ?, ?, ?)",
                (vt, skill, label, target),
            )


def _sqlite_has_column(conn, table, column):
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def _run_sqlite(path):
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cwip_cost_accumulation'"
    ).fetchone() is not None
    for stmt in _DDL:
        conn.execute(stmt)
    if not _sqlite_has_column(conn, _ASSET_TABLE, _ASSET_COLUMN):
        conn.execute(_SQLITE_ADD)
        print(f"  {_ASSET_TABLE}.{_ASSET_COLUMN}: added.")
    else:
        print(f"  {_ASSET_TABLE}.{_ASSET_COLUMN}: already present.")
    _seed(conn.execute)
    conn.commit()
    conn.close()
    print(f"  cwip_cost_accumulation: "
          f"{'already present' if existed else 'created'} (+ indexes, cwip_capitalization seed).")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for stmt in _DDL:
                cur.execute(stmt)
            cur.execute(_PG_ADD)
            print(f"  Postgres: {_ASSET_TABLE}.{_ASSET_COLUMN} ensured.")

            def _execute(sql, params=()):
                cur.execute(sql.replace("?", "%s"), params)
                return cur
            _seed(_execute)
        conn.commit()
        print("  Postgres: cwip_cost_accumulation ensured (+ indexes, cwip_capitalization seed).")
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
    parser = argparse.ArgumentParser(description="Migration 021: S3 CWIP cost accumulation")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 021 complete.")
