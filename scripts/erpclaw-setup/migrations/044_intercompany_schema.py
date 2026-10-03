"""Migration 044: create the intercompany linkage columns and the account-map table (M677).

The five intercompany actions (`add-intercompany-account-map`,
`list-intercompany-account-maps`, `create-intercompany-invoice`,
`list-intercompany-invoices`, `cancel-intercompany-invoice` in erpclaw-selling)
read and write `sales_invoice.is_intercompany` /
`sales_invoice.intercompany_reference_id`, the same two columns on
`purchase_invoice`, and the `intercompany_account_map` table — none of which
any schema file or earlier migration creates. On every install, fresh or
upgraded, the first of those actions therefore raises on the missing table or
columns before writing anything.

`init_schema.py` now declares all six objects on a fresh install (the two
columns appended after `updated_at` on each invoice table, so an upgraded table
carries the same column order; the map table directly after
`sales_invoice_item`'s indexes). This migration brings installs that already
exist to that same shape:

  1. ADD COLUMN `sales_invoice.is_intercompany` (nullable: no — NOT NULL
     DEFAULT 0 with the 0/1 CHECK, exactly as fresh installs declare it).
  2. ADD COLUMN `sales_invoice.intercompany_reference_id` (nullable TEXT).
  3. ADD COLUMN `purchase_invoice.is_intercompany` (same definition).
  4. ADD COLUMN `purchase_invoice.intercompany_reference_id` (nullable TEXT).
  5. CREATE TABLE `intercompany_account_map` (the object `init_schema.py`
     now creates, same definition and UNIQUE).

WHAT THIS DOES NOT DO: it does not touch a single row. The new columns arrive
with their defaults (`is_intercompany` 0, reference NULL) on every existing
row, and no statement here UPDATEs, DELETEs or backfills anything — there is
nothing to backfill, since no install could have recorded intercompany linkage
before the columns existed.

Idempotent: each column is added only when `seam.column_names` says it is
missing (per table, and only when the table itself is present), and the map
table is created only when `seam.table_exists` says it is absent. A second run
adds nothing, creates nothing, and says "already present".

CRASH SAFETY, STATED AS MEASURED RATHER THAN ASSUMED (see migration 036). The
run ends with one commit, but on SQLite a DDL statement issued while no
transaction is open self-commits outside it — so a crash between statements
can leave some columns added while later ones never ran. That state is safe:
every statement here is guarded by a catalog question, so a re-run adds
exactly what is still missing and finishes the job. The runner's "fix it and
re-run" instruction is safe to follow.

Authored through the seam (ADR-0034): `erpclaw_lib.db.get_connection` for the
connection, `erpclaw_lib.seam.table_exists` / `column_names` for the catalog
questions. No raw driver call, no connection setting, no catalog table read by
hand, so it runs unchanged on SQLite and PostgreSQL. Every statement is a
FIXED string (migration 031's rule): no table name, column name or value is
ever formatted into SQL.

Usage:
    python3 044_intercompany_schema.py [--db-path PATH]
"""
import argparse
import importlib.util
import os
import sys

# Deployed-lib bootstrap, guarded: production has nothing pre-imported so this
# resolves the installed lib, while a caller that already bound a tree (tests,
# the module runner inside a worktree) keeps its binding (ADR-0034 step 2d).
if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402

DEFAULT_DB_PATH = db_default()

# New columns and a new table; nothing a row held changes.
MIGRATION_DATA_CLASS = "none"

# Fixed statements, spelled out in full (migration 031's rule). The column
# text in each ADD COLUMN is character-identical to the column text
# init_schema.py declares; no name is ever formatted INTO a statement.
_ADD_SI_IS_INTERCOMPANY = ("ALTER TABLE sales_invoice ADD COLUMN "
                           "is_intercompany INTEGER NOT NULL DEFAULT 0 "
                           "CHECK(is_intercompany IN (0,1))")
_ADD_SI_REFERENCE = ("ALTER TABLE sales_invoice ADD COLUMN "
                     "intercompany_reference_id TEXT")
_ADD_PI_IS_INTERCOMPANY = ("ALTER TABLE purchase_invoice ADD COLUMN "
                           "is_intercompany INTEGER NOT NULL DEFAULT 0 "
                           "CHECK(is_intercompany IN (0,1))")
_ADD_PI_REFERENCE = ("ALTER TABLE purchase_invoice ADD COLUMN "
                     "intercompany_reference_id TEXT")
_CREATE_MAP_TABLE = (
    "CREATE TABLE IF NOT EXISTS intercompany_account_map (\n"
    "    id              TEXT PRIMARY KEY,\n"
    "    source_company_id TEXT NOT NULL REFERENCES company(id) ON DELETE RESTRICT,\n"
    "    target_company_id TEXT NOT NULL REFERENCES company(id) ON DELETE RESTRICT,\n"
    "    source_account_id TEXT NOT NULL REFERENCES account(id) ON DELETE RESTRICT,\n"
    "    target_account_id TEXT NOT NULL REFERENCES account(id) ON DELETE RESTRICT,\n"
    "    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,\n"
    "    UNIQUE(source_company_id, target_company_id, source_account_id)\n"
    ")")

_COLUMNS = (
    ("sales_invoice", "is_intercompany", _ADD_SI_IS_INTERCOMPANY),
    ("sales_invoice", "intercompany_reference_id", _ADD_SI_REFERENCE),
    ("purchase_invoice", "is_intercompany", _ADD_PI_IS_INTERCOMPANY),
    ("purchase_invoice", "intercompany_reference_id", _ADD_PI_REFERENCE),
)


def run_migration(db_path=None):
    # Every catalog question is asked BEFORE the first write. On PostgreSQL
    # an uncommitted ALTER TABLE holds a lock that a second connection's
    # catalog read blocks on until lock_timeout, so a read executed between
    # two writes (on the seam's own connection) would time out; deciding the
    # whole plan up front keeps the one commit at the end single and safe.
    planned = []
    for table, column, _statement in _COLUMNS:
        if not seam.table_exists(table, db_path):
            print("  %s absent on this install. Skipping its columns." % table)
            continue
        if column in seam.column_names(table, db_path):
            print("  %s.%s: already present" % (table, column))
            continue
        planned.append((table, column))
    create_map = not seam.table_exists("intercompany_account_map", db_path)
    if not create_map:
        print("  intercompany_account_map: already present")
    conn = get_connection(db_path)
    try:
        columns_added = []
        for table, column, statement in _COLUMNS:
            if (table, column) not in planned:
                continue
            conn.execute(statement)
            columns_added.append("%s.%s" % (table, column))
            print("  %s.%s: added." % (table, column))
        if create_map:
            conn.execute(_CREATE_MAP_TABLE)
            print("  intercompany_account_map: created.")
        conn.commit()
        if not columns_added and not create_map:
            print("  already present")
        return {"columns_added": columns_added, "table_created": create_map}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 044: add the intercompany linkage columns "
                    "and the account-map table")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("erpclaw-setup migration 044 complete.")
