"""Migration 025: Wave 2 M5 — putaway + pick list + persisted hard reservation.

Activates the warehouse-level putaway-routing + pick-list workflow + hard,
persisted stock reservations introduced by ADR-0026. Four net-new tables, all
owned + written exclusively by erpclaw-inventory's db_query.py; they are defined
in the foundation schema for fresh installs (init_schema.py INVENTORY_TABLES) and
added to existing DBs here, mirroring the M2 bank-statement precedent (020).

  - putaway_rule: warehouse-routing rule (item or item-group match → target
    warehouse). Match precedence: item match > item_group match; within a class,
    priority ASC. Soft-deleted via is_active=0.
  - pick_list: header for a SO-driven (or standalone) pick. Created BEFORE
    pick_list_item so the child's pick_list_id FK target exists at CREATE time
    (Postgres enforces FK targets at table creation).
  - pick_list_item: one row per item to pick. source_warehouse_bin is a free-TEXT
    hint only — there is no bin schema in V1 (ADR-0026 §3).
  - stock_reservation_entry: the hard-reservation row. status active/released/
    consumed; an active row reduces available qty and BLOCKS a conflicting
    consumption. get-projected-qty reads active rows as reserved_qty.

FK creation order matters: stock_reservation_entry / pick_list reference
item/warehouse/sales_order/company (all pre-exist in foundation); only the
intra-migration pick_list → pick_list_item edge is internal, so pick_list is
created before pick_list_item. Create order: putaway_rule → pick_list →
pick_list_item → stock_reservation_entry.

Pure CREATE TABLE / CREATE INDEX IF NOT EXISTS. Idempotent, dialect-aware, no
rebuild / no FK-rewrite trap. Columns match init_schema exactly. SIM-0-validated
(wave rehearsal harness): creation + FK ordering,
idempotency, cross-dialect shape, hard-reservation read path, get-projected-qty
back-compat all PASS.
"""
import argparse
import os
import sqlite3

# M102: adds tables / columns / indexes only — nothing a row held before this
# run is different afterwards.
MIGRATION_DATA_CLASS = "none"

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

# Order matters: parents before children (pick_list before pick_list_item).
_DDL = [
    """CREATE TABLE IF NOT EXISTS putaway_rule (
        id                  TEXT PRIMARY KEY,
        name                TEXT NOT NULL,
        priority            INTEGER NOT NULL DEFAULT 100,
        match_item_id       TEXT REFERENCES item(id) ON DELETE RESTRICT,
        match_item_group    TEXT,
        target_warehouse_id TEXT NOT NULL REFERENCES warehouse(id) ON DELETE RESTRICT,
        company_id          TEXT NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        is_active           INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
        created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at          TEXT DEFAULT CURRENT_TIMESTAMP,
        CHECK(match_item_id IS NOT NULL OR match_item_group IS NOT NULL)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_putaway_active ON putaway_rule(is_active, priority)",
    """CREATE TABLE IF NOT EXISTS pick_list (
        id                  TEXT PRIMARY KEY,
        name                TEXT NOT NULL,
        sales_order_id      TEXT REFERENCES sales_order(id) ON DELETE RESTRICT,
        from_warehouse_id   TEXT NOT NULL REFERENCES warehouse(id) ON DELETE RESTRICT,
        status              TEXT NOT NULL DEFAULT 'draft'
                            CHECK(status IN ('draft','submitted','picked','completed','cancelled')),
        picked_by_user_id   TEXT,
        picked_at           TEXT,
        company_id          TEXT NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at          TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pick_list_so ON pick_list(sales_order_id)",
    """CREATE TABLE IF NOT EXISTS pick_list_item (
        id                   TEXT PRIMARY KEY,
        pick_list_id         TEXT NOT NULL REFERENCES pick_list(id) ON DELETE RESTRICT,
        item_id              TEXT NOT NULL REFERENCES item(id) ON DELETE RESTRICT,
        expected_qty         TEXT NOT NULL DEFAULT '0',
        picked_qty           TEXT NOT NULL DEFAULT '0',
        source_warehouse_bin TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pick_list_item_list ON pick_list_item(pick_list_id)",
    """CREATE TABLE IF NOT EXISTS stock_reservation_entry (
        id                  TEXT PRIMARY KEY,
        voucher_type        TEXT NOT NULL CHECK(voucher_type IN ('sales_order','pick_list','manual')),
        voucher_id          TEXT,
        item_id             TEXT NOT NULL REFERENCES item(id) ON DELETE RESTRICT,
        warehouse_id        TEXT NOT NULL REFERENCES warehouse(id) ON DELETE RESTRICT,
        reserved_qty        TEXT NOT NULL DEFAULT '0',
        status              TEXT NOT NULL DEFAULT 'active'
                            CHECK(status IN ('active','released','consumed')),
        company_id          TEXT NOT NULL REFERENCES company(id) ON DELETE RESTRICT,
        reserved_at         TEXT DEFAULT CURRENT_TIMESTAMP,
        released_at         TEXT,
        consumed_at         TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_reservation_item_wh ON stock_reservation_entry(item_id, warehouse_id, status)",
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
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='putaway_rule'"
    ).fetchone() is not None
    for stmt in _DDL:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    print(f"  putaway_rule / pick_list / pick_list_item / stock_reservation_entry: "
          f"{'already present' if existed else 'created'} (+ indexes).")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for stmt in _DDL:
                cur.execute(stmt)
        conn.commit()
        print("  Postgres: putaway_rule / pick_list / pick_list_item / stock_reservation_entry ensured (+ indexes).")
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
    parser = argparse.ArgumentParser(description="Migration 025: Wave 2 M5 putaway/pick/reservation")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 025 complete.")
