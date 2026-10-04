"""Migration 053: store sessions, credentials, bootstrap challenges, deployments and issuers.

Fresh installs gain the five tables from ``init_schema``; this migration
brings an upgraded install to the same shape. It ensures whichever of
``authority_deployment``, ``authority_credential``, ``authority_session``,
``authority_bootstrap_challenge`` and ``operation_authorization_issuer`` are
absent, and adds the one nullable ``audit_log`` column
``actor_session_digest`` so later audit writes have somewhere to put the
session reference.

``MIGRATION_DATA_CLASS`` is ``"none"``: five new tables stay empty and one
nullable column is added; no value an install held is rewritten, no row is
added and none is removed.

The run refuses without the authority core and envelope (the session tables
point at them) and refuses on a wrong-shape session table: the shape check
runs before any ``audit_log`` column is added, so a refusal never adds an
audit column, though session tables that were absent may already have been
created (they are empty, and a rerun after the fix completes the upgrade).
"""
import argparse
import importlib.util
import os
import sys

# Data class: five new tables stay empty and one nullable column is added; no
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

AUDIT_COLUMNS = ("actor_session_digest",)

_ADD_ACTOR_SESSION_DIGEST = "ALTER TABLE audit_log ADD COLUMN actor_session_digest TEXT"

_ADD_STATEMENTS = {
    "actor_session_digest": _ADD_ACTOR_SESSION_DIGEST,
}


def _declared_partial(table):
    """Declared `(name, columns, predicate)` for each partial unique index."""
    entries = []
    for index in table.indexes:
        if not index.unique:
            continue
        where = None
        for dialect in ("sqlite", "postgresql"):
            options = index.dialect_options.get(dialect)
            if options is not None and options.get("where") is not None:
                where = options.get("where")
                break
        entries.append((index.name, tuple(column.name for column in index.columns),
                        seam._normalise_partial_predicate(where)))
    return sorted(entries)


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
    if len(live["checks"]) != want_checks:
        return False
    return sorted(live["partial_unique"]) == _declared_partial(table)


def run_migration(db_path=None, report_only=False):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    have = set(seam.table_names(path))
    absent = [name for name in seam._AUTHORITY_SESSION_TABLES
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
    seam.provision_authority_sessions(path)
    declared = seam.authority_session_metadata()
    for name in seam._AUTHORITY_SESSION_TABLES:
        if not _shape_ok(name, path, declared):
            raise RuntimeError(
                "authority sessions do not match their declaration: "
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
        description="Migration 053: store sessions, credentials, "
                    "bootstrap challenges, deployments and issuers")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    parser.add_argument("--report-only", action="store_true",
                        help="State what the real run would add; write nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("erpclaw-setup migration 053 "
          + ("report complete (no writes)." if args.report_only else "complete."))
