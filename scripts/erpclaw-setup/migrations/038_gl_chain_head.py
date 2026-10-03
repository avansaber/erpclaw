"""Migration 038: chain-head table for the GL checksum chain (m332a).

Gives every company a chain-head row holding the last handed-out sequence
value and the last checksum in its chain. New postings take the head under
the posting transaction, stamp ``gl_entry.sequence`` leg by leg, and write
the head back in the same transaction, so the build order and the verify
order are one explicit sequence on every backend.

What this run does, in order:

  * provisions ``gl_chain_head`` through ``erpclaw_lib.seam.provision``
    against the target the runner passes in, on either backend;
  * ensures the ``idx_gl_entry_sequence`` index the same way fresh installs
    get it;
  * counts, per company, chained legs still carrying no sequence, through a
    PyPika query, and prints each count.

On a backend where legacy insertion order is not recoverable those legacy
legs cannot be verified, so the report tells the operator to start from a
fresh database. The legacy legs themselves are left exactly as they are:
no leg is rewritten, no sequence is filled in afterwards, and no row of any
kind is added to any data table here.

Safe to run twice: provisioning skips what exists, the index statement is
conditional, and the counting pass changes nothing.
"""
import argparse
import importlib.util
import os
import sys

if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.query import Q, Table, fn, gl_legacy_order_recoverable  # noqa: E402

MIGRATION_DATA_CLASS = "none"

MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]

_CREATE_SEQUENCE_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_gl_entry_sequence ON gl_entry(sequence)")


def _legacy_counts(conn):
    """Per-company chained legs with no sequence, via PyPika."""
    g = Table("gl_entry")
    a = Table("account")
    q = (Q.from_(g).join(a).on(g.account_id == a.id)
         .select(a.company_id.as_("company_id"), fn.Count("*").as_("n"))
         .where(g.gl_checksum.isnotnull())
         .where(g.sequence.isnull())
         .groupby(a.company_id))
    return [(row["company_id"], row["n"])
            for row in conn.execute(q.get_sql()).fetchall()]


def run_migration(db_path=None, report_only=False):
    from erpclaw_lib.gl_chain_schema import METADATA

    if report_only:
        conn = get_connection(db_path)
        try:
            if not seam.table_exists("gl_entry", db_path):
                print("  gl_entry absent on this install. Nothing to do.")
                return {"provisioned": False, "index_created": False,
                        "legacy": [], "report_only": True,
                        "reason": "table absent"}
            counts = _legacy_counts(conn)
            try:
                index_created = ("idx_gl_entry_sequence"
                                 in seam.index_names("gl_entry", db_path))
            except Exception:
                index_created = False
            print("  report-only: nothing provisioned; nothing changed.")
            for company_id, n in counts:
                print("  company %s: %d chained leg(s) with no sequence"
                      % (company_id, n))
            if counts and not gl_legacy_order_recoverable():
                print("  WARNING: this backend cannot recover the write order "
                      "of those legacy legs, so they stay outside "
                      "verification; the database must be recreated — start "
                      "from a fresh database for a fully verifiable chain.")
            return {"provisioned": False, "index_created": index_created,
                    "legacy": counts, "report_only": True}
        finally:
            conn.close()
    seam.provision(METADATA, db_path)
    conn = get_connection(db_path)
    try:
        if not seam.table_exists("gl_entry", db_path):
            print("  gl_entry absent on this install. Nothing to do.")
            return {"provisioned": True, "index_created": True,
                    "legacy": [], "report_only": report_only,
                    "reason": "table absent"}
        conn.execute(_CREATE_SEQUENCE_INDEX)
        conn.commit()
        counts = _legacy_counts(conn)
        print("  gl_chain_head: ensured (+ sequence index).")
        if not counts:
            print("  no chained leg without a sequence; nothing further to do.")
        for company_id, n in counts:
            print("  company %s: %d chained leg(s) with no sequence"
                  % (company_id, n))
        if counts and not gl_legacy_order_recoverable():
            print("  WARNING: this backend cannot recover the write order "
                  "of those legacy legs, so they stay outside "
                  "verification; the database must be recreated — start "
                  "from a fresh database for a fully verifiable chain.")
        return {"provisioned": True, "index_created": True,
                "legacy": counts, "report_only": False}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--report-only", action="store_true",
                        help="State what the real run would ensure; change nothing.")
    args = parser.parse_args()
    run_migration(args.db_path, report_only=args.report_only)
    print("erpclaw-setup migration 038 "
          + ("report complete (no writes)." if args.report_only else "complete."))
