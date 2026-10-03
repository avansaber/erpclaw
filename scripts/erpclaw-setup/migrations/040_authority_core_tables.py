"""Migration 040: every install carries the eight authority-core tables.

Fresh installs gain the tables from init_schema; this migration brings an
upgraded install to the same shape. It adds whichever of the eight tables are
absent and ensures authority_install holds exactly one generated install
record.

MIGRATION_DATA_CLASS is "none" because the migration adds eight tables that
stay empty except for the single install record, whose content is generated
and depends on nothing the install held; no existing value is rewritten and
no row is removed.

A table of the same name with a different shape refuses the run: the
migration raises before any row is written and leaves only empty new tables
behind.
"""
import argparse
import importlib.util
import os
import sys

# M102: eight new tables, empty except the one install record, whose content
# is generated and depends on nothing the install held; no existing value is
# rewritten, no row removed.
MIGRATION_DATA_CLASS = "none"

# Deployed-lib bootstrap, guarded: production has nothing pre-imported so this
# resolves the installed lib, while a caller that already bound a tree (tests,
# the module runner inside a worktree) keeps its binding (ADR-0034 step 2d).
if importlib.util.find_spec("erpclaw_lib") is None:
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402
from erpclaw_lib.query import Q, Table  # noqa: E402

DEFAULT_DB_PATH = db_default()


def _shape_ok(name, path, declared):
    shape = seam.describe_table(name, path)
    table = declared.tables[name]
    live_columns = [(c["name"], str(c["type"]).upper(), c["nullable"])
                    for c in shape["columns"]]
    want_columns = [(c.name, seam.declared_type(c, path), bool(c.nullable))
                    for c in table.columns]
    live_pk = sorted(shape["primary_key"])
    want_pk = sorted(c.name for c in table.primary_key.columns)
    if live_columns != want_columns or live_pk != want_pk:
        return False
    live = seam.describe_constraints(name, path)
    _sa = seam._sqlalchemy()
    want_fks = sorted(
        (tuple(e.parent.name for e in c.elements),
         list(c.elements)[0].column.table.name,
         tuple(e.column.name for e in c.elements),
         seam._normalise_action(c.ondelete))
        for c in table.constraints
        if isinstance(c, _sa.ForeignKeyConstraint))
    if sorted(live["foreign_keys"]) != want_fks:
        return False
    want_uniques = sorted(
        tuple(col.name for col in c.columns)
        for c in table.constraints
        if isinstance(c, _sa.UniqueConstraint))
    if sorted(live["uniques"]) != want_uniques:
        return False
    want_checks = sum(
        1 for c in table.constraints
        if isinstance(c, _sa.CheckConstraint))
    return len(live["checks"]) == want_checks


def run_migration(db_path=None, report_only=False):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    have = set(seam.table_names(path))
    absent = [name for name in seam._AUTHORITY_CORE_TABLES if name not in have]
    if report_only:
        for name in absent:
            print("  %s: would be ensured." % name)
        print("  report-only: nothing was written.")
        return {"would_create": list(absent), "report_only": True}
    seam.provision_authority_core(path, seed=False)
    declared = seam.authority_core_metadata()
    for name in seam._AUTHORITY_CORE_TABLES:
        if not _shape_ok(name, path, declared):
            raise RuntimeError(
                "authority core does not match its profile: STRUCTURE")
    seeded = seam.provision_authority_core(path)
    conn = get_connection(path)
    try:
        probe = Table("authority_install")
        rows = conn.execute(
            Q.from_(probe).select(probe.singleton).get_sql()).fetchall()
        if len(rows) != 1:
            raise RuntimeError(
                "authority core does not match its profile: INSTALL")
    finally:
        conn.close()
    for name in absent:
        print("  %s: ensured." % name)
    return {"created": list(absent),
            "install_seeded": bool(seeded["install_seeded"]),
            "status": "CHECKED", "report_only": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 040: ship the authority-core tables "
                    "to upgraded installs")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="State which tables would be ensured; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("erpclaw-setup migration 040 "
          + ("report complete (no writes)." if args.report_only else "complete."))
