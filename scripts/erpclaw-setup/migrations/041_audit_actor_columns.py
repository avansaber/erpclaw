"""Migration 041: record the actor context on every new audit row.

Adds five nullable columns to `audit_log`, in this order: `actor_os_account`,
`actor_channel`, `actor_principal_claim`, `actor_status` and `actor_hop`. From
here on the business audit path stores who the process ran as and what
identity it claimed alongside the nine values it already wrote. Rows written
before this migration hold NULL in all five columns, meaning recording did not
exist yet; that is different from a later row with no passed context, which
records its own absent marker.

`MIGRATION_DATA_CLASS` is `"none"`: five nullable columns are added and no
existing value is rewritten, no row is added and none is removed. The
statements below only widen the table so later writes have somewhere to put
the context.

Authored through the seam: `erpclaw_lib.db.get_connection` for the handle and
`erpclaw_lib.seam.table_exists` / `column_names` for the catalog questions, so
it runs unchanged on both backends. Every statement is a FIXED string: no
table name, column name or value is ever formatted into SQL.

Usage:
    python3 041_audit_actor_columns.py [--db-path PATH] [--report-only]
"""
import argparse
import importlib.util
import os
import sys

MIGRATION_DATA_CLASS = "none"

if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402

DEFAULT_DB_PATH = db_default()

TABLE = "audit_log"

ACTOR_COLUMNS = ("actor_os_account", "actor_channel", "actor_principal_claim",
                 "actor_status", "actor_hop")

_ADD_ACTOR_OS_ACCOUNT = "ALTER TABLE audit_log ADD COLUMN actor_os_account TEXT"
_ADD_ACTOR_CHANNEL = "ALTER TABLE audit_log ADD COLUMN actor_channel TEXT"
_ADD_ACTOR_PRINCIPAL_CLAIM = "ALTER TABLE audit_log ADD COLUMN actor_principal_claim TEXT"
_ADD_ACTOR_STATUS = "ALTER TABLE audit_log ADD COLUMN actor_status TEXT"
_ADD_ACTOR_HOP = "ALTER TABLE audit_log ADD COLUMN actor_hop TEXT"

_ADD_STATEMENTS = {
    "actor_os_account": _ADD_ACTOR_OS_ACCOUNT,
    "actor_channel": _ADD_ACTOR_CHANNEL,
    "actor_principal_claim": _ADD_ACTOR_PRINCIPAL_CLAIM,
    "actor_status": _ADD_ACTOR_STATUS,
    "actor_hop": _ADD_ACTOR_HOP,
}


def run_migration(db_path=None, report_only=False):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    conn = get_connection(path)
    try:
        if not seam.table_exists(TABLE, path):
            print(f"  {TABLE} absent on this install. Nothing to do.")
            return {"added": [], "report_only": report_only,
                    "reason": "table absent"}
        missing = [name for name in ACTOR_COLUMNS
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
        description="Migration 041: add actor context columns to audit_log")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="State what the real run would add; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("erpclaw-setup migration 041 "
          + ("report complete (no writes)." if args.report_only else "complete."))
