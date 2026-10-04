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

# M102: adds tables / columns / indexes only — nothing a row held before this
# run is different afterwards.
MIGRATION_DATA_CLASS = "none"

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

_DUNNING_LEVEL_DDL = """
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
        """

_DUNNING_RUN_DDL = """
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
        """

_ADD_CREDIT_STATUS_PG = "ALTER TABLE customer ADD COLUMN credit_status TEXT NOT NULL DEFAULT 'active' CHECK(credit_status IN ('active','on_hold','suspended'))"


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _run_postgres(db_path):
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    import erpclaw_lib.db as _db
    import erpclaw_lib.seam as _seam
    _customer_exists = _seam.table_exists("customer", db_path)
    if _customer_exists:
        _customer_columns = _seam.column_names("customer", db_path)
    else:
        _customer_columns = []
    add_credit_status = _customer_exists and "credit_status" not in _customer_columns
    create_level = not _seam.table_exists("dunning_level", db_path)
    create_run = not _seam.table_exists("dunning_run", db_path)
    conn = _db.get_connection(db_path)
    try:
        if add_credit_status:
            conn.execute(_ADD_CREDIT_STATUS_PG)
            print("  PostgreSQL: customer.credit_status: added.")
        elif not _customer_exists:
            print("  PostgreSQL: customer absent; customer.credit_status not added")
        else:
            print("  PostgreSQL: customer.credit_status: already present")
        if create_level:
            conn.execute(_DUNNING_LEVEL_DDL)
            print("  PostgreSQL: dunning_level: created.")
        else:
            print("  PostgreSQL: dunning_level: already present")
        if create_run:
            conn.execute(_DUNNING_RUN_DDL)
            print("  PostgreSQL: dunning_run: created.")
        else:
            print("  PostgreSQL: dunning_run: already present")
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        raise
    conn.close()


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
    if _get_dialect() == "postgresql":
        return _run_postgres(db_path)
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    if not os.path.exists(path):
        print(f"Database not found at {path}. Nothing to migrate.")
        return

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
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
        conn.executescript(_DUNNING_LEVEL_DDL)
        print("  ensured dunning_level table")

        # Step 3: Create dunning_run table
        conn.executescript(_DUNNING_RUN_DDL)
        print("  ensured dunning_run table")

        conn.commit()
        print("Migration 002 complete.")
    finally:
        conn.close()


def _build_parser():
    """Build the command-line parser, resolving the default at call time.

    On a PostgreSQL dialect the configured URL is the target, so `--db-path`
    defaults to None; on SQLite it defaults to the install database file.
    """
    if os.environ.get("ERPCLAW_DB_DIALECT", "sqlite") == "postgresql":
        _default = None
    else:
        _default = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")
    _parser = argparse.ArgumentParser(description=__doc__)
    _parser.add_argument("--db-path", default=_default,
                         help="Database path (defaults to the install database file on SQLite)")
    return _parser


def main():
    run_migration(_build_parser().parse_args().db_path)


if __name__ == "__main__":
    main()
