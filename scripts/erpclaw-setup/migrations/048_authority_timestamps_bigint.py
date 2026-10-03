"""Migration 048: authority timestamps hold real millisecond clock values.

Every authority timestamp is a millisecond epoch value, about 1.8 x 10^12
today. The narrow four-byte storage an older install carries cannot hold such
a value, so issuing or consuming an authorization with a real clock reading
fails there. This migration widens the thirteen timestamp columns across the
six authority tables to eight-byte storage. The declared metadata already
asks for eight-byte storage where the backend distinguishes widths, and stays
unchanged elsewhere; installs created after this change already carry the
wide columns, so the migration is a no-op for them.

`MIGRATION_DATA_CLASS` is `"none"`: a type widening keeps every value, so no
row is rewritten, added or removed.

Authored through the seam: the handle comes from
`erpclaw_lib.db.get_connection`, the backend from
`erpclaw_lib.db.get_dialect`, and catalog questions go only through
`erpclaw_lib.seam` (`table_exists`, `describe_table`). Every statement is a
FIXED string: no table name, column name or value is ever formatted into SQL.

Usage:
    python3 048_authority_timestamps_bigint.py [--db-path PATH] [--report-only]
"""
import argparse
import importlib.util
import os
import sys

# A type widening keeps every value; no row is rewritten, added or removed.
MIGRATION_DATA_CLASS = "none"

if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection, get_dialect  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402

DEFAULT_DB_PATH = db_default()

_ALTER_AUTHORITY_PRINCIPAL_DISABLED_AT = "ALTER TABLE authority_principal ALTER COLUMN disabled_at TYPE BIGINT"
_ALTER_AUTHORITY_DELEGATION_ISSUED_AT = "ALTER TABLE authority_delegation ALTER COLUMN issued_at TYPE BIGINT"
_ALTER_AUTHORITY_DELEGATION_EXPIRES_AT = "ALTER TABLE authority_delegation ALTER COLUMN expires_at TYPE BIGINT"
_ALTER_AUTHORITY_DELEGATION_REVOKED_AT = "ALTER TABLE authority_delegation ALTER COLUMN revoked_at TYPE BIGINT"
_ALTER_AUTHORITY_DELEGATION_CAP_WINDOW_START = "ALTER TABLE authority_delegation_cap ALTER COLUMN window_start TYPE BIGINT"
_ALTER_AUTHORITY_DELEGATION_CAP_WINDOW_END = "ALTER TABLE authority_delegation_cap ALTER COLUMN window_end TYPE BIGINT"
_ALTER_OPERATION_AUTHORIZATION_ISSUED_AT = "ALTER TABLE operation_authorization ALTER COLUMN issued_at TYPE BIGINT"
_ALTER_OPERATION_AUTHORIZATION_EXPIRES_AT = "ALTER TABLE operation_authorization ALTER COLUMN expires_at TYPE BIGINT"
_ALTER_OPERATION_AUTHORIZATION_REVOKED_AT = "ALTER TABLE operation_authorization ALTER COLUMN revoked_at TYPE BIGINT"
_ALTER_OPERATION_AUTHORIZATION_CONSUMED_AT = "ALTER TABLE operation_authorization ALTER COLUMN consumed_at TYPE BIGINT"
_ALTER_OPERATION_AUTHORIZATION_RESULT_RECORDED_AT = "ALTER TABLE operation_authorization_result ALTER COLUMN recorded_at TYPE BIGINT"
_ALTER_AUTHORITY_DELEGATION_USAGE_WINDOW_START = "ALTER TABLE authority_delegation_usage ALTER COLUMN window_start TYPE BIGINT"
_ALTER_AUTHORITY_DELEGATION_USAGE_WINDOW_END = "ALTER TABLE authority_delegation_usage ALTER COLUMN window_end TYPE BIGINT"

ALTER_STATEMENTS = {
    ("authority_principal", "disabled_at"): _ALTER_AUTHORITY_PRINCIPAL_DISABLED_AT,
    ("authority_delegation", "issued_at"): _ALTER_AUTHORITY_DELEGATION_ISSUED_AT,
    ("authority_delegation", "expires_at"): _ALTER_AUTHORITY_DELEGATION_EXPIRES_AT,
    ("authority_delegation", "revoked_at"): _ALTER_AUTHORITY_DELEGATION_REVOKED_AT,
    ("authority_delegation_cap", "window_start"): _ALTER_AUTHORITY_DELEGATION_CAP_WINDOW_START,
    ("authority_delegation_cap", "window_end"): _ALTER_AUTHORITY_DELEGATION_CAP_WINDOW_END,
    ("operation_authorization", "issued_at"): _ALTER_OPERATION_AUTHORIZATION_ISSUED_AT,
    ("operation_authorization", "expires_at"): _ALTER_OPERATION_AUTHORIZATION_EXPIRES_AT,
    ("operation_authorization", "revoked_at"): _ALTER_OPERATION_AUTHORIZATION_REVOKED_AT,
    ("operation_authorization", "consumed_at"): _ALTER_OPERATION_AUTHORIZATION_CONSUMED_AT,
    ("operation_authorization_result", "recorded_at"): _ALTER_OPERATION_AUTHORIZATION_RESULT_RECORDED_AT,
    ("authority_delegation_usage", "window_start"): _ALTER_AUTHORITY_DELEGATION_USAGE_WINDOW_START,
    ("authority_delegation_usage", "window_end"): _ALTER_AUTHORITY_DELEGATION_USAGE_WINDOW_END,
}


def _planned_alterations(path):
    """Dotted `table.column` names still on narrow storage, in widen order."""
    by_table = {}
    for table, column in ALTER_STATEMENTS:
        by_table.setdefault(table, []).append(column)
    planned = []
    for table, columns in by_table.items():
        if not seam.table_exists(table, path):
            continue
        shape = seam.describe_table(table, path)
        live = {entry["name"]: str(entry["type"]).upper()
                for entry in shape["columns"]}
        for column in columns:
            if column not in live:
                raise RuntimeError("authority timestamps: STRUCTURE")
            seen = live[column]
            if seen == "BIGINT":
                continue
            if seen != "INTEGER":
                raise RuntimeError("authority timestamps: STRUCTURE")
            planned.append(table + "." + column)
    return planned


def run_migration(db_path=None, report_only=False):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    if get_dialect() == "sqlite":
        print("  authority timestamps already fit this backend. Nothing to do.")
        return {"altered": [], "report_only": report_only}
    planned = _planned_alterations(path)
    if report_only:
        for name in planned:
            print("  %s: would be widened." % name)
        return {"would_alter": planned, "report_only": True}
    if not planned:
        return {"altered": [], "report_only": False}
    wanted = {"%s.%s" % key: ALTER_STATEMENTS[key]
              for key in ALTER_STATEMENTS}
    conn = get_connection(path)
    try:
        try:
            for name in planned:
                conn.execute(wanted[name])
            conn.commit()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            print({"status": "error", "message": str(exc)})
            raise SystemExit(1)
    finally:
        conn.close()
    for name in planned:
        print("  %s: widened." % name)
    return {"altered": planned, "report_only": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 048: widen authority timestamps to eight bytes")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="State what the real run would widen; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("erpclaw-setup migration 048 "
          + ("report complete (no writes)." if args.report_only else "complete."))
