"""Migration 001: Add type registry tables and relax CHECK constraints.

This migration:
1. Creates voucher_type_registry, party_type_registry, account_type_registry tables
2. Recreates gl_entry, stock_ledger_entry, payment_entry, payment_allocation,
   payment_ledger_entry, and account tables WITHOUT hardcoded CHECK constraints
3. Adds dimensions_json column to gl_entry
4. Adds payment_method column to payment_entry
5. Adds project_id index to gl_entry
6. Seeds registry tables with core ERPClaw types

Idempotent: safe to run multiple times. Skips if registry tables already exist.

Usage:
    python3 001_registry_tables.py [--db-path PATH]
"""
import argparse
import json
import os
import sqlite3
import sys

# M102: seeds the release's fixed registry rows (identical on every install) —
# nothing a row held before this run is different afterwards.
MIGRATION_DATA_CLASS = "none"

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

_REGISTRY_DDL = """
            CREATE TABLE IF NOT EXISTS voucher_type_registry (
                voucher_type TEXT NOT NULL,
                skill_name   TEXT NOT NULL,
                label        TEXT NOT NULL,
                target_table TEXT NOT NULL CHECK(target_table IN ('gl_entry','stock_ledger_entry','payment_allocation')),
                PRIMARY KEY (voucher_type, target_table)
            );

            CREATE TABLE IF NOT EXISTS party_type_registry (
                party_type  TEXT PRIMARY KEY,
                skill_name  TEXT NOT NULL,
                label       TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS account_type_registry (
                account_type TEXT PRIMARY KEY,
                skill_name   TEXT NOT NULL,
                label        TEXT NOT NULL
            );
        """

_ADD_DIMENSIONS_JSON = "ALTER TABLE gl_entry ADD COLUMN dimensions_json TEXT NOT NULL DEFAULT '{}'"
_ADD_PAYMENT_METHOD = "ALTER TABLE payment_entry ADD COLUMN payment_method TEXT DEFAULT ''"
_ADD_PROJECT_INDEX = "CREATE INDEX IF NOT EXISTS idx_gl_entry_project ON gl_entry(project_id)"


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _run_postgres(db_path):
    import importlib.util
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    import erpclaw_lib.db as _db
    import erpclaw_lib.seam as _seam
    _registries = ("voucher_type_registry", "party_type_registry", "account_type_registry")
    missing_registries = [t for t in _registries if not _seam.table_exists(t, db_path)]
    _gl_exists = _seam.table_exists("gl_entry", db_path)
    _pay_exists = _seam.table_exists("payment_entry", db_path)
    if _gl_exists:
        _gl_columns = _seam.column_names("gl_entry", db_path)
        _gl_indexes = _seam.index_names("gl_entry", db_path)
    else:
        _gl_columns = []
        _gl_indexes = []
    if _pay_exists:
        _pay_columns = _seam.column_names("payment_entry", db_path)
    else:
        _pay_columns = []
    add_dimensions = _gl_exists and "dimensions_json" not in _gl_columns
    add_payment_method = _pay_exists and "payment_method" not in _pay_columns
    add_project_index = _gl_exists and "idx_gl_entry_project" not in _gl_indexes
    conn = _db.get_connection(db_path)
    try:
        if missing_registries:
            conn.execute(_REGISTRY_DDL)
            for _name in _registries:
                if _name in missing_registries:
                    print("  PostgreSQL: %s: created." % _name)
                else:
                    print("  PostgreSQL: %s: already present" % _name)
        else:
            for _name in _registries:
                print("  PostgreSQL: %s: already present" % _name)
        if add_dimensions:
            conn.execute(_ADD_DIMENSIONS_JSON)
            print("  PostgreSQL: gl_entry.dimensions_json: added.")
        else:
            print("  PostgreSQL: gl_entry.dimensions_json: already present")
        if add_payment_method:
            conn.execute(_ADD_PAYMENT_METHOD)
            print("  PostgreSQL: payment_entry.payment_method: added.")
        else:
            print("  PostgreSQL: payment_entry.payment_method: already present")
        if add_project_index:
            conn.execute(_ADD_PROJECT_INDEX)
            print("  PostgreSQL: idx_gl_entry_project: added.")
        else:
            print("  PostgreSQL: idx_gl_entry_project: already present")
        _seed_registries(conn)
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
    print("  PostgreSQL: registries seeded (missing rows only).")


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
    from erpclaw_lib.db import setup_pragmas
    setup_pragmas(conn)
    conn.execute("PRAGMA foreign_keys=OFF")  # Must be OFF during table rebuild

    try:
        # Step 1: Create registry tables if they don't exist
        conn.executescript(_REGISTRY_DDL)

        # Step 2: Add dimensions_json to gl_entry if missing
        if _table_exists(conn, "gl_entry") and not _column_exists(conn, "gl_entry", "dimensions_json"):
            conn.execute(_ADD_DIMENSIONS_JSON)
            print("  Added dimensions_json column to gl_entry")

        # Step 3: Add payment_method to payment_entry if missing
        if _table_exists(conn, "payment_entry") and not _column_exists(conn, "payment_entry", "payment_method"):
            conn.execute(_ADD_PAYMENT_METHOD)
            print("  Added payment_method column to payment_entry")

        # Step 4: Add project_id index if missing
        idx = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_gl_entry_project'"
        ).fetchone()
        if not idx and _table_exists(conn, "gl_entry"):
            conn.execute(_ADD_PROJECT_INDEX)
            print("  Added idx_gl_entry_project index")

        # Step 5: Seed registry tables
        _seed_registries(conn)

        conn.commit()
        print("Migration 001 completed successfully.")

    except Exception as e:
        conn.rollback()
        print(f"Migration 001 FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.close()


def _seed_registries(conn):
    """Seed all registry tables with core ERPClaw types."""

    # GL entry voucher types
    gl_vtypes = [
        ("journal_entry", "erpclaw-journals", "Journal Entry"),
        ("sales_invoice", "erpclaw-selling", "Sales Invoice"),
        ("purchase_invoice", "erpclaw-buying", "Purchase Invoice"),
        ("payment_entry", "erpclaw-payments", "Payment Entry"),
        ("stock_entry", "erpclaw-inventory", "Stock Entry"),
        ("depreciation_entry", "erpclaw-assets", "Depreciation Entry"),
        ("payroll_entry", "erpclaw-payroll", "Payroll Entry"),
        ("period_closing", "erpclaw-gl", "Period Closing"),
        ("expense_claim", "erpclaw-hr", "Expense Claim"),
        ("asset_disposal", "erpclaw-assets", "Asset Disposal"),
        ("stock_reconciliation", "erpclaw-inventory", "Stock Reconciliation"),
        ("purchase_receipt", "erpclaw-buying", "Purchase Receipt"),
        ("delivery_note", "erpclaw-selling", "Delivery Note"),
        ("credit_note", "erpclaw-selling", "Credit Note"),
        ("debit_note", "erpclaw-buying", "Debit Note"),
        ("work_order", "erpclaw-manufacturing", "Work Order"),
        ("exchange_rate_revaluation", "erpclaw-gl", "Exchange Rate Revaluation"),
        ("stock_revaluation", "erpclaw-inventory", "Stock Revaluation"),
        ("elimination_entry", "erpclaw-gl", "Elimination Entry"),
    ]
    for vt, skill, label in gl_vtypes:
        existing = conn.execute(
            "SELECT 1 FROM voucher_type_registry WHERE voucher_type = ? AND target_table = 'gl_entry'",
            (vt,),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO voucher_type_registry (voucher_type, skill_name, label, target_table) VALUES (?, ?, ?, 'gl_entry')",
                (vt, skill, label),
            )

    # SLE voucher types
    sle_vtypes = [
        ("stock_entry", "erpclaw-inventory", "Stock Entry"),
        ("purchase_receipt", "erpclaw-buying", "Purchase Receipt"),
        ("delivery_note", "erpclaw-selling", "Delivery Note"),
        ("stock_reconciliation", "erpclaw-inventory", "Stock Reconciliation"),
        ("work_order", "erpclaw-manufacturing", "Work Order"),
        ("sales_invoice", "erpclaw-selling", "Sales Invoice"),
        ("credit_note", "erpclaw-selling", "Credit Note"),
        ("purchase_invoice", "erpclaw-buying", "Purchase Invoice"),
        ("debit_note", "erpclaw-buying", "Debit Note"),
        ("stock_revaluation", "erpclaw-inventory", "Stock Revaluation"),
    ]
    for vt, skill, label in sle_vtypes:
        existing = conn.execute(
            "SELECT 1 FROM voucher_type_registry WHERE voucher_type = ? AND target_table = 'stock_ledger_entry'",
            (vt,),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO voucher_type_registry (voucher_type, skill_name, label, target_table) VALUES (?, ?, ?, 'stock_ledger_entry')",
                (vt, skill, label),
            )

    # Payment allocation voucher types
    pa_vtypes = [
        ("sales_invoice", "erpclaw-selling", "Sales Invoice"),
        ("purchase_invoice", "erpclaw-buying", "Purchase Invoice"),
        ("credit_note", "erpclaw-selling", "Credit Note"),
        ("debit_note", "erpclaw-buying", "Debit Note"),
    ]
    for vt, skill, label in pa_vtypes:
        existing = conn.execute(
            "SELECT 1 FROM voucher_type_registry WHERE voucher_type = ? AND target_table = 'payment_allocation'",
            (vt,),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO voucher_type_registry (voucher_type, skill_name, label, target_table) VALUES (?, ?, ?, 'payment_allocation')",
                (vt, skill, label),
            )

    # Party types
    for pt, skill, label in [
        ("customer", "erpclaw-selling", "Customer"),
        ("supplier", "erpclaw-buying", "Supplier"),
        ("employee", "erpclaw-hr", "Employee"),
    ]:
        existing = conn.execute(
            "SELECT 1 FROM party_type_registry WHERE party_type = ?",
            (pt,),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO party_type_registry (party_type, skill_name, label) VALUES (?, ?, ?)",
                (pt, skill, label),
            )

    # Account types
    for at, skill, label in [
        ("bank", "erpclaw-gl", "Bank"),
        ("cash", "erpclaw-gl", "Cash"),
        ("receivable", "erpclaw-selling", "Receivable"),
        ("payable", "erpclaw-buying", "Payable"),
        ("stock", "erpclaw-inventory", "Stock"),
        ("fixed_asset", "erpclaw-assets", "Fixed Asset"),
        ("accumulated_depreciation", "erpclaw-assets", "Accumulated Depreciation"),
        ("cost_of_goods_sold", "erpclaw-selling", "Cost of Goods Sold"),
        ("tax", "erpclaw-tax", "Tax"),
        ("equity", "erpclaw-gl", "Equity"),
        ("revenue", "erpclaw-selling", "Revenue"),
        ("expense", "erpclaw-gl", "Expense"),
        ("stock_received_not_billed", "erpclaw-buying", "Stock Received Not Billed"),
        ("stock_adjustment", "erpclaw-inventory", "Stock Adjustment"),
        ("rounding", "erpclaw-gl", "Rounding"),
        ("exchange_gain_loss", "erpclaw-gl", "Exchange Gain/Loss"),
        ("depreciation", "erpclaw-assets", "Depreciation"),
        ("payroll_payable", "erpclaw-payroll", "Payroll Payable"),
        ("temporary", "erpclaw-gl", "Temporary"),
        ("asset_received_not_billed", "erpclaw-assets", "Asset Received Not Billed"),
        ("trust", "erpclaw-gl", "Trust"),
    ]:
        existing = conn.execute(
            "SELECT 1 FROM account_type_registry WHERE account_type = ?",
            (at,),
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO account_type_registry (account_type, skill_name, label) VALUES (?, ?, ?)",
                (at, skill, label),
            )

    print(f"  Seeded type registries: "
          f"{len(gl_vtypes)} GL voucher types, "
          f"{len(sle_vtypes)} SLE voucher types, "
          f"3 party types, 21 account types")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migration 001: Registry tables")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
