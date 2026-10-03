"""Migration 045: store authorization envelopes, their results and usage.

Fresh installs gain the three tables from ``init_schema``; this migration
brings an upgraded install to the same shape. It ensures whichever of
``operation_authorization_envelope``, ``operation_authorization_result`` and
``authority_delegation_usage`` are absent, and adds the two nullable
``audit_log`` columns ``authorization_id`` and ``authorization_status`` so
later audit writes have somewhere to put the envelope reference.

``MIGRATION_DATA_CLASS`` is ``"none"``: three new tables stay empty and two
nullable columns are added; no value an install held is rewritten, no row is
added and none is removed.

The run refuses without the authority core (the envelope tables point at it)
and refuses on a wrong-shape envelope table: the shape check runs before any
``audit_log`` column is added, so a refusal never adds an audit column, though
envelope tables that were absent may already have been created (they are empty,
and a rerun after the fix completes the upgrade).
"""
import argparse
import importlib.util
import os
import sys

# Data class: three new tables stay empty and two nullable columns are added; no
# existing value is rewritten, no row is added and none is removed.
MIGRATION_DATA_CLASS = "none"

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

AUDIT_COLUMNS = ("authorization_id", "authorization_status")

_ADD_AUTHORIZATION_ID = "ALTER TABLE audit_log ADD COLUMN authorization_id TEXT"
_ADD_AUTHORIZATION_STATUS = "ALTER TABLE audit_log ADD COLUMN authorization_status TEXT"

_ADD_STATEMENTS = {
    "authorization_id": _ADD_AUTHORIZATION_ID,
    "authorization_status": _ADD_AUTHORIZATION_STATUS,
}


def _declared_shapes(path=None):
    declared = seam.authority_envelope_metadata()
    shapes = {}
    for name in seam._AUTHORITY_ENVELOPE_TABLES:
        table = declared.tables[name]
        columns = [(column.name, seam.declared_type(column, path),
                    bool(column.nullable)) for column in table.columns]
        key = sorted(column.name for column in table.primary_key.columns)
        shapes[name] = (columns, key)
    return shapes


def run_migration(db_path=None, report_only=False):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    have = set(seam.table_names(path))
    absent = [name for name in seam._AUTHORITY_ENVELOPE_TABLES
              if name not in have]
    if seam.table_exists("audit_log", path):
        missing = [name for name in AUDIT_COLUMNS
                   if name not in seam.column_names("audit_log", path)]
    else:
        missing = []
    if report_only:
        for name in absent:
            print("  %s: would be ensured." % name)
        for name in missing:
            print("  audit_log.%s: would be added." % name)
        print("  report-only: nothing was written.")
        return {"would_create": list(absent), "would_add": list(missing),
                "report_only": True}
    seam.provision_authority_envelope(path)
    declared = _declared_shapes(path)
    for name in seam._AUTHORITY_ENVELOPE_TABLES:
        shape = seam.describe_table(name, path)
        live = ([(column["name"], str(column["type"]).upper(),
                  column["nullable"]) for column in shape["columns"]],
                sorted(shape["primary_key"]))
        if live != declared[name]:
            raise RuntimeError(
                "authorization envelope does not match its declaration: "
                "STRUCTURE")
    conn = get_connection(path)
    try:
        for name in missing:
            conn.execute(_ADD_STATEMENTS[name])
        conn.commit()
    finally:
        conn.close()
    for name in absent:
        print("  %s: ensured." % name)
    for name in missing:
        print("  audit_log.%s: added." % name)
    return {"created": list(absent), "added": list(missing),
            "report_only": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 045: store authorization envelopes, "
                    "their results and delegation usage")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="State what the real run would add; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("erpclaw-setup migration 045 "
          + ("report complete (no writes)." if args.report_only else "complete."))
