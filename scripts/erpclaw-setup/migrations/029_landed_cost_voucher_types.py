"""Migration 029: Register the landed_cost_voucher voucher type (D1, WS1 v4.13.0).

`add-landed-cost-voucher` posts GL (DR Stock-in-Hand / CR expense) through
`gl_posting.insert_gl_entries`, which since the M0-phase-2 registry displacement
(migration 004, 2026-05-31) enforces `voucher_type` validity against
`voucher_type_registry` (target_table='gl_entry'). But `landed_cost_voucher` was
never seeded, so every landed-cost GL post failed the gate — a pre-existing
latent bug masked by the action having had zero real tests. D1 completes the
feature (SLE valuation half + list/get/cancel), which makes this gap load-bearing:
the add/cancel actions cannot function until the type is registered.

This seeds the two rows that init_schema.VOUCHER_TYPE_REGISTRY_SEED now carries
for fresh installs, so existing DBs match:

  - ('landed_cost_voucher', 'erpclaw-buying', 'Landed Cost Voucher', 'gl_entry')
        required: the GL post goes through insert_gl_entries' registry gate.
  - ('landed_cost_voucher', 'erpclaw-buying', 'Landed Cost Voucher',
        'stock_ledger_entry')
        registry honesty: landed-cost now writes zero-qty valuation SLE rows
        (the repricing helper inserts them directly, like revalue-stock, so this
        row is not gate-enforced — but the registry documents who writes each
        table, and leaving it out is exactly the drift M0 built the registry to
        prevent).

Data-seed only — no table/column DDL. Idempotent (guarded insert / ON CONFLICT
DO NOTHING), dialect-aware. Forward-only; nothing to roll back but the two rows.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

# (voucher_type, skill_name, label, target_table)
_SEED = [
    ("landed_cost_voucher", "erpclaw-buying", "Landed Cost Voucher", "gl_entry"),
    ("landed_cost_voucher", "erpclaw-buying", "Landed Cost Voucher", "stock_ledger_entry"),
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
    seeded = 0
    for vt, skill, label, target in _SEED:
        if not conn.execute(
            "SELECT 1 FROM voucher_type_registry WHERE voucher_type = ? AND target_table = ?",
            (vt, target),
        ).fetchone():
            conn.execute(
                "INSERT INTO voucher_type_registry (voucher_type, skill_name, label, target_table) "
                "VALUES (?, ?, ?, ?)",
                (vt, skill, label, target),
            )
            seeded += 1
    conn.commit()
    conn.close()
    print(f"  landed_cost_voucher voucher types seeded ({seeded} new, {len(_SEED) - seeded} already present).")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for vt, skill, label, target in _SEED:
                cur.execute(
                    "INSERT INTO voucher_type_registry (voucher_type, skill_name, label, target_table) "
                    "VALUES (%s, %s, %s, %s) ON CONFLICT (voucher_type, target_table) DO NOTHING",
                    (vt, skill, label, target),
                )
        conn.commit()
        print("  Postgres: landed_cost_voucher voucher types seeded (if absent).")
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
    parser = argparse.ArgumentParser(description="Migration 029: register landed_cost_voucher voucher type")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 029 complete.")
