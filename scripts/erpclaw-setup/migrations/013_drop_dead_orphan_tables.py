"""Migration 013: Drop confirmed-dead orphan tables (audit P2).

Seven foundation tables were defined in init_schema but had ZERO code references
anywhere (planned-but-unbuilt features). Removed from init_schema for fresh
installs; this drops them from existing DBs so fresh == migrated.

Dropped (children before parents to respect FKs):
  product_bundle_item, product_bundle            (bundle pricing — never built)
  employee_tax_exemption_declaration, _category  (India-style payroll — never built)
  supplier_score                                 (vendor scoring — never built)
  property_setter                                (Frappe-style field overrides — never wired)
  user_permission                                (per-entity ACL — RBAC uses role/role_permission)

NOT touched: integration tables (plaid_*/stripe_*/s3_backup_record), highered/
commercial verticals (may be staged for in-flight work), OS-governance tables, and
`communication` (name collides with the English word; ambiguous) — all pending
product-intent confirmation.

Idempotent (DROP IF EXISTS), dialect-aware. Forward-only: these tables are empty
(no code ever wrote them), so there is no data to preserve.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")

# children/referencing tables first
_DROP_ORDER = [
    "product_bundle_item",
    "product_bundle",
    "employee_tax_exemption_declaration",
    "employee_tax_exemption_category",
    "supplier_score",
    "property_setter",
    "user_permission",
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
    dropped = []
    for t in _DROP_ORDER:
        existed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        conn.execute(f"DROP TABLE IF EXISTS {t}")
        if existed:
            dropped.append(t)
    conn.commit()
    conn.close()
    print(f"  dropped: {', '.join(dropped) if dropped else '(none — already absent)'}")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for t in _DROP_ORDER:
                cur.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
        print("  Postgres: dead orphan tables dropped (if present).")
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
    parser = argparse.ArgumentParser(description="Migration 013: drop dead orphan tables")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 013 complete.")
