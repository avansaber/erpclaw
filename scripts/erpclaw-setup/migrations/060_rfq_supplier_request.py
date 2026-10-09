"""Provision unsent Buying preparations without changing existing data."""
import argparse
import importlib.util
import os
import sys

if importlib.util.find_spec("erpclaw_lib") is None:
    sys.path.insert(0, os.path.join(os.path.expanduser(
        os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam
from erpclaw_lib.rfq_supplier_request_schema import METADATA
from erpclaw_lib.paths import db_default

MIGRATION_DATA_CLASS = "none"


def run_migration(db_path=None):
    path = db_path or db_default()
    for name in ("company", "request_for_quotation"):
        if not seam.table_exists(name, path):
            raise RuntimeError("Initialize company and RFQ schema before this migration")
    seam.provision(METADATA, path)
    if not {"id", "company_id", "rfq_id", "prepared_at", "snapshot",
            "content_sha256"} <= set(seam.column_names("rfq_supplier_request", path)):
        raise RuntimeError("rfq_supplier_request does not match its declaration")
    return {"provisioned": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default=db_default())
    run_migration(parser.parse_args().db_path)


if __name__ == "__main__":
    main()
