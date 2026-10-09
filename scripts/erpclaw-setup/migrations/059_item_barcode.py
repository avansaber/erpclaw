"""Ensure the inventory barcode store without changing existing item data."""
import argparse
import importlib.util
import os
import sys

if importlib.util.find_spec("erpclaw_lib") is None:
    sys.path.insert(0, os.path.join(os.path.expanduser(
        os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam
from erpclaw_lib.item_barcode_schema import METADATA
from erpclaw_lib.paths import db_default

MIGRATION_DATA_CLASS = "none"


def run_migration(db_path=None):
    path = db_path or db_default()
    if not seam.table_exists("company", path) or not seam.table_exists("item", path):
        raise RuntimeError("Initialize the company and item schema before barcode migration")
    seam.provision(METADATA, path)
    columns = set(seam.column_names("item_barcode", path))
    if not {"id", "company_id", "item_id", "barcode"} <= columns:
        raise RuntimeError("item_barcode does not match its declaration")
    return {"provisioned": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=db_default())
    run_migration(parser.parse_args().db_path)


if __name__ == "__main__":
    main()
