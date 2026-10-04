"""Migration 054: replay 001 and 002 on PostgreSQL installs that skipped them.

Before m842, foundation migrations 001 and 002 treated a PostgreSQL URL as a
file path, printed "Database not found ... Nothing to migrate.", and returned
normally, so the runner ledgered both as applied without adding anything. A
PostgreSQL install upgraded with that code still carries both rows as applied
yet still lacks what they add: the added columns on gl_entry, payment_entry
and customer, the project index on gl_entry, the seeded registry rows, and the
two dunning tables.

This migration holds no SQL of its own: on PostgreSQL it loads 001 and 002
from this directory and calls their PostgreSQL routines with the target
unchanged, then verifies every object through the seam and refuses (raising,
so the runner records 054 failed) while anything is still absent. It never
records itself applied with an object still missing. Re-running is safe: both
routines only add what is absent, so a second run is a no-op.

MIGRATION_DATA_CLASS is "none" because the data class rests on 001's and
002's: both declare "none" for the same fixed catalog rows this repair replays
(identical on every install, rewriting no existing value). A seed row an
operator deleted comes back with is_active defaulting to 1; it is a release
catalog row, so the class stays "none".

Usage:
    python3 054_foundation_001_002_on_postgresql.py [--db-path PATH]
"""
import argparse
import importlib.util
import os
import sys

# M102: this replays the fixed catalog rows 001 and 002 ship (identical on
# every install) and rewrites no existing value — see the module docstring.
MIGRATION_DATA_CLASS = "none"

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

_REGISTRIES = ("voucher_type_registry", "party_type_registry", "account_type_registry")

_PRECHECK_MESSAGE = ("054: a type registry is absent; this install did not complete "
                     "migration 008 and cannot be repaired by 054")


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _load_sibling(module_name, filename):
    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location(module_name, os.path.join(here, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _probe_columns(seam, table, db_path):
    try:
        return list(seam.column_names(table, db_path))
    except Exception:
        return None


def _probe_indexes(seam, table, db_path):
    try:
        return list(seam.index_names(table, db_path))
    except Exception:
        return None


def _missing_objects(seam, db_path):
    missing = []
    gl_columns = _probe_columns(seam, "gl_entry", db_path)
    if not gl_columns or "dimensions_json" not in gl_columns:
        missing.append("gl_entry.dimensions_json")
    pay_columns = _probe_columns(seam, "payment_entry", db_path)
    if not pay_columns or "payment_method" not in pay_columns:
        missing.append("payment_entry.payment_method")
    customer_columns = _probe_columns(seam, "customer", db_path)
    if not customer_columns or "credit_status" not in customer_columns:
        missing.append("customer.credit_status")
    gl_indexes = _probe_indexes(seam, "gl_entry", db_path)
    if not gl_indexes or "idx_gl_entry_project" not in gl_indexes:
        missing.append("idx_gl_entry_project")
    if not seam.table_exists("dunning_level", db_path):
        missing.append("dunning_level")
    if not seam.table_exists("dunning_run", db_path):
        missing.append("dunning_run")
    for registry in _REGISTRIES:
        registry_columns = _probe_columns(seam, registry, db_path)
        if not registry_columns or "is_active" not in registry_columns:
            missing.append("%s.is_active" % registry)
    return missing


def run_migration(db_path=None):
    if _get_dialect() != "postgresql":
        print("  SQLite: nothing to repair (001 and 002 never skipped on SQLite).")
        return
    if importlib.util.find_spec("erpclaw_lib") is None:
        sys.path.insert(0, os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))
    import erpclaw_lib.seam as _seam
    for registry in _REGISTRIES:
        if not _seam.table_exists(registry, db_path):
            raise RuntimeError(_PRECHECK_MESSAGE)
    first = _load_sibling("erpclaw_m054_m001", "001_registry_tables.py")
    second = _load_sibling("erpclaw_m054_m002", "002_credit_dunning.py")
    first._run_postgres(db_path)
    second._run_postgres(db_path)
    missing = _missing_objects(_seam, db_path)
    if missing:
        raise RuntimeError("054: %s absent after repair" % ", ".join(missing))


def _build_parser():
    """Build the command-line parser, resolving the default at call time.

    On a PostgreSQL dialect the configured URL is the target, so `--db-path`
    defaults to None; on SQLite it defaults to the install database file.
    """
    if os.environ.get("ERPCLAW_DB_DIALECT", "sqlite") == "postgresql":
        _default = None
    else:
        _default = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")
    _parser = argparse.ArgumentParser(description=__doc__)
    _parser.add_argument("--db-path", default=_default,
                         help="Database path (defaults to the install database file on SQLite)")
    return _parser


if __name__ == "__main__":
    run_migration(_build_parser().parse_args().db_path)
