"""Migration 039: receipt lines and bill lines carry their share of the order-line discount.

The purchase-order line records a discount, and the receipt line and the bill
line it turns into had nowhere to hold their share of it. Fresh installs
declare the column; this file adds it on installs that already exist:

  1. ADD COLUMN `purchase_receipt_item.discount_amount` (money text, zero by
     default).
  2. ADD COLUMN `purchase_invoice_item.discount_amount` (money text, zero by
     default).

WHY THIS DECLARES "none". Each new column arrives with a constant default, so
every existing row reads the same value on every install with nothing written
to it: no existing value is rewritten, and no row is added or removed. There
is no backfill because there is nothing to compute — a fresh install and a
migrated one hold the same columns in the same order with the same defaults.

A crash between the two ADDs is recovered by rerunning: each ADD is guarded
on its own column, so the half that already landed reads as present and only
the missing half is added.

Authored through the seam: `erpclaw_lib.db.get_connection` for the
connection, `erpclaw_lib.seam.table_exists` / `column_names` for the catalog
questions. Every statement is a FIXED string: no table name, column name or
value is ever formatted into SQL, so this runs unchanged on both backends.

Usage:
    python3 039_purchase_line_discount_columns.py [--db-path PATH] [--report-only]
"""
import argparse
import importlib.util
import os
import sys

# Deployed-lib bootstrap, guarded: production has nothing pre-imported so this
# resolves the installed lib, while a caller that already bound a tree (tests,
# the module runner inside a worktree) keeps its binding.
if importlib.util.find_spec("erpclaw_lib") is None:
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402

DEFAULT_DB_PATH = db_default()

# This migration changes no stored data: new columns with a constant default,
# identical on every install.
MIGRATION_DATA_CLASS = "none"

TABLES = ("purchase_receipt_item", "purchase_invoice_item")

COLUMN = "discount_amount"

# Fixed statements, spelled out in full. The constants above name the same
# objects for the seam calls and the printed messages, but no name is ever
# formatted INTO a statement.
_ADD_RECEIPT_ITEM = "ALTER TABLE purchase_receipt_item ADD COLUMN discount_amount TEXT NOT NULL DEFAULT '0'"
_ADD_INVOICE_ITEM = "ALTER TABLE purchase_invoice_item ADD COLUMN discount_amount TEXT NOT NULL DEFAULT '0'"


def run_migration(db_path=None, report_only=False):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    conn = get_connection(path)
    added, present, absent = [], [], []
    try:
        for table in TABLES:
            if not seam.table_exists(table, path):
                print(f"  {table} absent on this install. Nothing to do.")
                absent.append(table)
                continue
            if COLUMN in seam.column_names(table, path):
                print(f"  {table}.{COLUMN}: already present")
                present.append(table)
                continue
            if report_only:
                print(f"  {table}.{COLUMN}: would be added "
                      "(ALTER TABLE ... ADD COLUMN)")
                continue
            if table == "purchase_receipt_item":
                conn.execute(_ADD_RECEIPT_ITEM)
            else:
                conn.execute(_ADD_INVOICE_ITEM)
            print(f"  {table}.{COLUMN}: added.")
            added.append(table)
        if not report_only:
            conn.commit()
    finally:
        conn.close()
    return {"added": added, "present": present, "absent": absent,
            "report_only": report_only}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 039: receipt lines and bill lines carry "
                    "their share of the order-line discount")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="State what the real run would add; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("erpclaw-setup migration 039 "
          + ("report complete (no writes)." if args.report_only else "complete."))
