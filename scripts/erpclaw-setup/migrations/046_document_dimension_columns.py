"""Migration 046: business documents carry their accounting dimensions.

A business document (journal entry, quotation, sales order, sales invoice,
delivery note, purchase order, purchase receipt, purchase invoice, landed
cost voucher, payment, stock entry, stock reconciliation, stock revaluation,
expense claim, payroll run, and the three recurring templates) carries a set
of accounting dimensions, checked when the draft is written and copied onto
every ledger row the document posts. Fresh installs declare the
`dimensions_json` column last on each of those tables; this file adds the
same column, last, on installs that already exist (one ADD COLUMN per
table, nineteen in all).

WHY THIS DECLARES "none". Each new column arrives with a constant default,
so every existing row reads the same value on every install with nothing
written to it: no existing value is rewritten, and no row is added or
removed. There is no backfill because there is nothing to compute — a fresh
install and a migrated one hold the same columns in the same order with the
same defaults.

A crash between the ADDs is recovered by rerunning: each ADD is guarded on
its own column, so the half that already landed reads as present and only
the missing half is added. On a busy PostgreSQL server the run can hit the
lock timeout; a rerun is the recovery.

Authored through the seam: `erpclaw_lib.db.get_connection` for the
connection, `erpclaw_lib.seam.table_exists` / `column_names` for the catalog
questions. Every statement is a FIXED string: no table name, column name or
value is ever formatted into SQL, so this runs unchanged on both backends.

Usage:
    python3 046_document_dimension_columns.py [--db-path PATH] [--report-only]
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

TABLES = ("journal_entry", "journal_entry_line", "recurring_journal_template",
          "payment_entry", "quotation", "sales_order", "delivery_note",
          "sales_invoice", "recurring_invoice_template", "purchase_order",
          "purchase_receipt", "purchase_invoice", "landed_cost_voucher",
          "recurring_bill_template", "stock_entry", "stock_reconciliation",
          "stock_revaluation", "expense_claim", "payroll_run")

COLUMN = "dimensions_json"

# Fixed statements, spelled out in full. The constants above name the same
# objects for the seam calls and the printed messages, but no name is ever
# formatted INTO a statement.
_ADD_JOURNAL_ENTRY = "ALTER TABLE journal_entry ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_JOURNAL_ENTRY_LINE = "ALTER TABLE journal_entry_line ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_RECURRING_JOURNAL_TEMPLATE = "ALTER TABLE recurring_journal_template ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_PAYMENT_ENTRY = "ALTER TABLE payment_entry ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_QUOTATION = "ALTER TABLE quotation ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_SALES_ORDER = "ALTER TABLE sales_order ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_DELIVERY_NOTE = "ALTER TABLE delivery_note ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_SALES_INVOICE = "ALTER TABLE sales_invoice ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_RECURRING_INVOICE_TEMPLATE = "ALTER TABLE recurring_invoice_template ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_PURCHASE_ORDER = "ALTER TABLE purchase_order ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_PURCHASE_RECEIPT = "ALTER TABLE purchase_receipt ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_PURCHASE_INVOICE = "ALTER TABLE purchase_invoice ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_LANDED_COST_VOUCHER = "ALTER TABLE landed_cost_voucher ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_RECURRING_BILL_TEMPLATE = "ALTER TABLE recurring_bill_template ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_STOCK_ENTRY = "ALTER TABLE stock_entry ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_STOCK_RECONCILIATION = "ALTER TABLE stock_reconciliation ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_STOCK_REVALUATION = "ALTER TABLE stock_revaluation ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_EXPENSE_CLAIM = "ALTER TABLE expense_claim ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_PAYROLL_RUN = "ALTER TABLE payroll_run ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"

_STATEMENTS = {
    "journal_entry": _ADD_JOURNAL_ENTRY,
    "journal_entry_line": _ADD_JOURNAL_ENTRY_LINE,
    "recurring_journal_template": _ADD_RECURRING_JOURNAL_TEMPLATE,
    "payment_entry": _ADD_PAYMENT_ENTRY,
    "quotation": _ADD_QUOTATION,
    "sales_order": _ADD_SALES_ORDER,
    "delivery_note": _ADD_DELIVERY_NOTE,
    "sales_invoice": _ADD_SALES_INVOICE,
    "recurring_invoice_template": _ADD_RECURRING_INVOICE_TEMPLATE,
    "purchase_order": _ADD_PURCHASE_ORDER,
    "purchase_receipt": _ADD_PURCHASE_RECEIPT,
    "purchase_invoice": _ADD_PURCHASE_INVOICE,
    "landed_cost_voucher": _ADD_LANDED_COST_VOUCHER,
    "recurring_bill_template": _ADD_RECURRING_BILL_TEMPLATE,
    "stock_entry": _ADD_STOCK_ENTRY,
    "stock_reconciliation": _ADD_STOCK_RECONCILIATION,
    "stock_revaluation": _ADD_STOCK_REVALUATION,
    "expense_claim": _ADD_EXPENSE_CLAIM,
    "payroll_run": _ADD_PAYROLL_RUN,
}


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
            conn.execute(_STATEMENTS[table])
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
        description="Migration 046: business documents carry their "
                    "accounting dimensions")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="State what the real run would add; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("erpclaw-setup migration 046 "
          + ("report complete (no writes)." if args.report_only else "complete."))
