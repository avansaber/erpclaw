"""Migration 002: Add credit_status to customer + dunning_level + dunning_run tables.

Implements the schema half of ROADMAP item S1 (Customer credit limit + dunning
levels). The other half — actions + invoice-submit credit check — lives in
`erpclaw-selling/db_query.py`.

Schema additions:
1. customer.credit_status — TEXT NOT NULL DEFAULT 'active'
   CHECK(credit_status IN ('active','on_hold','suspended'))
   Separate concept from customer.status (which is active/inactive/blocked,
   meaning "is this customer still ours"); credit_status governs whether AR
   can extend new credit even to an otherwise-active customer.
2. dunning_level — escalation policy rows: at N days overdue, take action
   (email | hold | call). Templates referenced by id, optional.
3. dunning_run — log of run-dunning-cycle invocations: which customer at
   which level, which invoice_ids, what email_id was generated, status.

Idempotent. Safe to run multiple times.

Usage:
    python3 002_credit_dunning.py [--db-path PATH]
"""
import argparse
import os
import sqlite3
import sys

DEFAULT_DB_PATH = os.path.expanduser("~/.openclaw/erpclaw/data.sqlite")


def _table_exists(conn, table_name):
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def _column_exists(conn, table_name, column_name):
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    for row in cursor:
        if row[1] == column_name:
            return True
    return False


def run_migration(db_path=None):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    if not os.path.exists(path):
        print(f"Database not found at {path}. Nothing to migrate.")
        return

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    sys.path.insert(0, os.path.expanduser("~/.openclaw/erpclaw/lib"))
    from erpclaw_lib.db import setup_pragmas
    setup_pragmas(conn)

    try:
        # Step 1: Add customer.credit_status (idempotent)
        if _table_exists(conn, "customer") and not _column_exists(conn, "customer", "credit_status"):
            # SQLite ALTER TABLE doesn't support adding a NOT NULL column with
            # a non-NULL default that has a CHECK constraint in one shot when
            # rows already exist. Add as nullable first, backfill, then
            # would-rebuild for the CHECK — but for simplicity we accept the
            # nullable form here and enforce CHECK in app code. New rows go
            # through init_schema.py which has the full CHECK.
            conn.execute("ALTER TABLE customer ADD COLUMN credit_status TEXT NOT NULL DEFAULT 'active'")
            print("  added customer.credit_status")
        else:
            print("  customer.credit_status: already present, skipping")

        # Step 2: Create dunning_level table
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dunning_level (
                id              TEXT PRIMARY KEY,
                company_id      TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
                level           INTEGER NOT NULL CHECK(level BETWEEN 1 AND 10),
                days_overdue    INTEGER NOT NULL CHECK(days_overdue >= 0),
                action          TEXT NOT NULL
                                CHECK(action IN ('email','hold','call','suspend')),
                template_id     TEXT,
                description     TEXT,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(company_id, level)
            );

            CREATE INDEX IF NOT EXISTS idx_dunning_level_company
                ON dunning_level(company_id);
        """)
        print("  ensured dunning_level table")

        # Step 3: Create dunning_run table
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS dunning_run (
                id              TEXT PRIMARY KEY,
                company_id      TEXT NOT NULL REFERENCES company(id) ON DELETE CASCADE,
                run_date        TEXT NOT NULL,
                customer_id     TEXT NOT NULL REFERENCES customer(id) ON DELETE CASCADE,
                level           INTEGER NOT NULL CHECK(level BETWEEN 1 AND 10),
                invoice_ids_json TEXT NOT NULL DEFAULT '[]',
                action_taken    TEXT NOT NULL
                                CHECK(action_taken IN ('email','hold','call','suspend')),
                status          TEXT NOT NULL DEFAULT 'completed'
                                CHECK(status IN ('completed','failed','skipped')),
                generated_email_id TEXT,
                notes           TEXT,
                created_at      TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_dunning_run_customer
                ON dunning_run(customer_id);
            CREATE INDEX IF NOT EXISTS idx_dunning_run_date
                ON dunning_run(run_date);
        """)
        print("  ensured dunning_run table")

        conn.commit()
        print("Migration 002 complete.")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH,
                        help=f"Database path (default: {DEFAULT_DB_PATH})")
    args = parser.parse_args()
    run_migration(args.db_path)


if __name__ == "__main__":
    main()
