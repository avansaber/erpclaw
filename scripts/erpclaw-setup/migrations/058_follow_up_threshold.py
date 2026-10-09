"""Migration 058: follow-up agent Pack 1 threshold store.

Provisions ``follow_up_threshold``, one active staleness-threshold row per
company for the v1 deterministic follow-up cycle (``set-follow-up-threshold``
/ ``run-follow-up-cycle`` in erpclaw-selling). Fresh installs gain the same
shape from ``init_schema``; this run brings an upgraded install level.

The run provisions through ``erpclaw_lib.seam.provision`` against the target
the runner passes in, on either backend, then verifies the shape through the
seam and refuses (raising, so the runner records 058 failed) while anything
is still absent. Re-running is safe: provisioning skips what exists.

MIGRATION_DATA_CLASS is "none": one empty store is ensured; no value an
install held is rewritten, no row is added and none is removed.

Usage:
    python3 058_follow_up_threshold.py [--db-path PATH]
"""
import argparse
import importlib.util
import os
import sys

if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402
from erpclaw_lib.seam import (  # noqa: E402
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
    reference_table,
)

# Data class: one empty store is ensured; no existing value is rewritten,
# no row is added and none is removed.
MIGRATION_DATA_CLASS = "none"

# Derived, never typed: the runner ledgers this file under its stem, and
# `get-system-audit-log --audit-action migration:<stem>` has to match that exact string.
MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]

DEFAULT_DB_PATH = db_default()

METADATA = MetaData()

reference_table("company", METADATA)

FOLLOW_UP_THRESHOLD = Table(
    "follow_up_threshold", METADATA,
    Column("id", Text, primary_key=True),
    Column("company_id", Text, ForeignKey("company.id", ondelete="CASCADE"),
           nullable=False),
    Column("days_stale", Integer,
           CheckConstraint("days_stale BETWEEN 1 AND 366"), nullable=False),
    Column("is_active", Integer,
           CheckConstraint("is_active IN (0, 1)"),
           nullable=False, default=1),
    Column("created_at", Text),
    Column("updated_at", Text),
    UniqueConstraint("company_id"),
    Index("idx_follow_up_threshold_company", "company_id"),
)


def _shape_ok(path):
    columns = {c["name"] for c in seam.describe_table(
        "follow_up_threshold", path)["columns"]}
    return {"id", "company_id", "days_stale", "is_active",
            "created_at", "updated_at"} <= columns


def run_migration(db_path=None):
    path = db_path or DEFAULT_DB_PATH
    if not seam.table_exists("company", path):
        print("  company absent on this install. Nothing to do.")
        return {"provisioned": False, "reason": "company absent"}
    seam.provision(METADATA, path)
    if not seam.table_exists("follow_up_threshold", path) or not _shape_ok(path):
        raise RuntimeError(
            "follow_up_threshold does not match its declaration: STRUCTURE")
    print("  follow_up_threshold: ensured.")
    print("Migration 058 complete.")
    return {"provisioned": True}


def _build_parser():
    _parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    _parser.add_argument("--db-path", default=DEFAULT_DB_PATH,
                         help="Database path (defaults to the install database file on SQLite)")
    return _parser


def main():
    run_migration(_build_parser().parse_args().db_path)


if __name__ == "__main__":
    main()
