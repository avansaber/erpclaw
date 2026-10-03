"""Migration 047: record the company scope on every new audit row.

Adds two nullable columns to `audit_log`, in this order: `scope_company_ids`
and `scope_status`. From here on the business audit path stores which
companies a call touched, de-duplicated, sorted and joined with `,`, alongside
the scope check outcome, which is exactly one of `in_scope`, `out_of_scope`,
`no_scope`, `no_principal`, `underived` or `not_applicable`. Rows written
before this migration hold NULL in both columns, meaning recording did not
exist yet; that is different from a later row with no passed scope, which
holds NULL because nothing was recorded for it.

`MIGRATION_DATA_CLASS` is `"none"`: two nullable columns are added and no
existing value is rewritten, no row is added and none is removed. The
statements below only widen the table so later writes have somewhere to put
the scope verdict.

Authored through the seam: `erpclaw_lib.db.get_connection` for the handle and
`erpclaw_lib.seam.table_exists` / `column_names` for the catalog questions, so
it runs unchanged on both backends. Every statement is a FIXED string: no
table name, column name or value is ever formatted into SQL.

Usage:
    python3 047_audit_company_scope.py [--db-path PATH] [--report-only]
"""
import argparse
import importlib.util
import os
import sys

# Data class: two nullable columns are added and no existing value is
# rewritten, no row is added and none is removed.
MIGRATION_DATA_CLASS = "none"

if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402

DEFAULT_DB_PATH = db_default()

TABLE = "audit_log"

SCOPE_COLUMNS = ("scope_company_ids", "scope_status")

_ADD_SCOPE_COMPANY_IDS = "ALTER TABLE audit_log ADD COLUMN scope_company_ids TEXT"
_ADD_SCOPE_STATUS = "ALTER TABLE audit_log ADD COLUMN scope_status TEXT"

_ADD_STATEMENTS = {
    "scope_company_ids": _ADD_SCOPE_COMPANY_IDS,
    "scope_status": _ADD_SCOPE_STATUS,
}


def run_migration(db_path=None, report_only=False):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    conn = get_connection(path)
    try:
        if not seam.table_exists(TABLE, path):
            print(f"  {TABLE} absent on this install. Nothing to do.")
            return {"added": [], "report_only": report_only,
                    "reason": "table absent"}
        missing = [name for name in SCOPE_COLUMNS
                   if name not in seam.column_names(TABLE, path)]
        if report_only:
            for name in missing:
                print(f"  {TABLE}.{name}: would be added.")
            return {"would_add": missing, "report_only": True}
        for name in missing:
            conn.execute(_ADD_STATEMENTS[name])
        conn.commit()
        for name in missing:
            print(f"  {TABLE}.{name}: added.")
        return {"added": missing, "report_only": False}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 047: add company scope columns to audit_log")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="State what the real run would add; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("erpclaw-setup migration 047 "
          + ("report complete (no writes)." if args.report_only else "complete."))
