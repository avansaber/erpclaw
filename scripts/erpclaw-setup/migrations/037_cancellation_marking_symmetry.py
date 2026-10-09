"""Migration 037: mark BOTH legs of a cancellation, on installs that carry the split (M135).

`reverse_gl_entries` inserted the reversal leg with `is_cancelled=0` while
setting `is_cancelled=1` on the original. Every reader applying the house
`is_cancelled = 0` filter therefore dropped the original and KEPT the mirror,
counting the cancellation once instead of netting it to zero. Measured on the P3
rehearsal box: Trade Receivables read **-1,150.00** filtered where the truth was
**-300.00** — one whole 850.00 cancellation counted as if it had happened.

Totals balanced in BOTH views (3430=3430 unfiltered, 2280=2280 filtered), because
the filter removes a balanced PAIR across two accounts. That is why no
totals-based invariant saw it, and why INV-30 (per-voucher marking symmetry) had
to be written before this migration could be trusted to know what it was healing.

The code fix stops new splits. This heals the ones already on disk.

WHY THIS COVERS BOTH LEDGERS THOUGH ONLY ONE WAS BROKEN. `reverse_sle_entries`
already marks both legs, verified in source and against live rows. It is included
anyway because older versions of that code are not visible from here, and the
report-only pass proving **zero** on stock_ledger_entry across real installs is
worth more than an assumption that it was always correct. A migration that finds
nothing, and says so, is evidence.

PAIR-VERIFIED, AND WHAT THAT REFUSES TO DO. A voucher is healed only when its
rows, taken together, balance to zero — the signature of a complete
cancel-by-reversal. A voucher that is partly marked AND does not balance is not a
split cancellation; it is something this migration does not understand, and it is
REPORTED AND SKIPPED rather than marked. Marking those would convert an unknown
state into a confidently wrong one, which is the failure this whole row exists to
undo.

REPORT-ONLY IS THE DELIVERABLE. `--report-only` is the first-class path and the
default for judging an install: it counts and names what would change, writes
nothing, and writes no audit rows. Run it, read it, then run the write.
"""
import argparse
import importlib.util
import os
import sys
from decimal import Decimal

# Deployed-lib bootstrap, guarded: production has nothing pre-imported so this
# resolves the installed lib, while a caller that already bound a tree (tests,
# the module runner inside a worktree) keeps its binding (ADR-0034 step 2d).
if importlib.util.find_spec("erpclaw_lib") is None:  # pragma: no cover - env-dependent
    sys.path.insert(0, os.path.join(
        os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "lib"))

from erpclaw_lib import seam  # noqa: E402
from erpclaw_lib.audit import audit_migration  # noqa: E402
from erpclaw_lib.db import get_connection  # noqa: E402
from erpclaw_lib.paths import db_default  # noqa: E402

DEFAULT_DB_PATH = db_default()

# Derived, never typed: the runner ledgers this file under its stem, and
# `get-system-audit-log --audit-action migration:<stem>` has to match that exact string.
MIGRATION_ID = os.path.splitext(os.path.basename(__file__))[0]

# M102: this REWRITES a value in a column that existed before the run, on rows
# whose contents depend on what this install happens to hold. That is the
# definition of data-changing, without judgement — so it writes the trail.
MIGRATION_DATA_CLASS = "rows"

MIGRATION_DATA_EXEMPTIONS = {}

# Fixed statements, spelled out in full (migration 031's rule): no name is ever
# formatted INTO a statement, because an f-string that assembles SQL is
# indistinguishable from an injection site to the Article-10 scanner.
_SPLIT_GL = (
    "SELECT voucher_type, voucher_id, "
    "       SUM(CASE WHEN is_cancelled = 1 THEN 1 ELSE 0 END) AS marked, "
    "       COUNT(*) AS total "
    "FROM gl_entry GROUP BY voucher_type, voucher_id "
    "HAVING SUM(CASE WHEN is_cancelled = 1 THEN 1 ELSE 0 END) > 0 "
    "AND SUM(CASE WHEN is_cancelled = 1 THEN 1 ELSE 0 END) < COUNT(*)")
_SPLIT_SLE = (
    "SELECT voucher_type, voucher_id, "
    "       SUM(CASE WHEN is_cancelled = 1 THEN 1 ELSE 0 END) AS marked, "
    "       COUNT(*) AS total "
    "FROM stock_ledger_entry GROUP BY voucher_type, voucher_id "
    "HAVING SUM(CASE WHEN is_cancelled = 1 THEN 1 ELSE 0 END) > 0 "
    "AND SUM(CASE WHEN is_cancelled = 1 THEN 1 ELSE 0 END) < COUNT(*)")
_GL_ROWS = ("SELECT debit, credit FROM gl_entry "
            "WHERE voucher_type = ? AND voucher_id = ?")
_SLE_ROWS = ("SELECT actual_qty FROM stock_ledger_entry "
             "WHERE voucher_type = ? AND voucher_id = ?")
_HEAL_GL = ("UPDATE gl_entry SET is_cancelled = 1 "
            "WHERE voucher_type = ? AND voucher_id = ? AND is_cancelled = 0")
_HEAL_SLE = ("UPDATE stock_ledger_entry SET is_cancelled = 1 "
             "WHERE voucher_type = ? AND voucher_id = ? AND is_cancelled = 0")


def _balances_gl(conn, vtype, vid):
    """Do the voucher's rows net to zero? The signature of a complete reversal."""
    dr = cr = Decimal("0")
    for row in conn.execute(_GL_ROWS, (vtype, vid)).fetchall():
        dr += Decimal(str(row[0] or "0"))
        cr += Decimal(str(row[1] or "0"))
    return dr == cr


def _balances_sle(conn, vtype, vid):
    qty = Decimal("0")
    for row in conn.execute(_SLE_ROWS, (vtype, vid)).fetchall():
        qty += Decimal(str(row[0] or "0"))
    return qty == Decimal("0")


def run_migration(db_path=None, report_only=False):
    conn = get_connection(db_path)
    healed, skipped, per_table = [], [], {}
    try:
        for table, split_sql, heal_sql, balances in (
                ("gl_entry", _SPLIT_GL, _HEAL_GL, _balances_gl),
                ("stock_ledger_entry", _SPLIT_SLE, _HEAL_SLE, _balances_sle)):
            if not seam.table_exists(table, db_path):
                per_table[table] = {"split": 0, "healed": 0, "skipped": 0,
                                    "absent": True}
                continue
            rows = conn.execute(split_sql).fetchall()
            t_healed = t_skipped = 0
            for r in rows:
                vtype, vid = r[0], r[1]
                marked, total = r[2], r[3]
                if not balances(conn, vtype, vid):
                    # Partly marked AND unbalanced: not a split cancellation.
                    # Reported, never marked — see the docstring.
                    skipped.append({"table": table, "voucher_type": vtype,
                                    "voucher_id": vid, "marked": marked,
                                    "total": total,
                                    "reason": "rows do not net to zero"})
                    t_skipped += 1
                    continue
                if not report_only:
                    conn.execute(heal_sql, (vtype, vid))
                    # One audit row per changed DOCUMENT, on this connection,
                    # inside this transaction — never after the commit.
                    audit_migration(
                        conn, MIGRATION_ID, table, str(vid),
                        old_values={"is_cancelled":
                                    "0 on %d of %d rows" % (total - marked, total)},
                        new_values={"is_cancelled": "1 on all %d rows" % total},
                        description=("cancellation marking made symmetric "
                                     "(M135): %s" % vtype))
                healed.append({"table": table, "voucher_type": vtype,
                               "voucher_id": vid, "rows_marked": total - marked})
                t_healed += 1
            per_table[table] = {"split": len(rows), "healed": t_healed,
                                "skipped": t_skipped, "absent": False}
        if not report_only:
            conn.commit()
    finally:
        conn.close()
    return {"migration": MIGRATION_ID, "report_only": report_only,
            "per_table": per_table, "healed": healed, "skipped": skipped}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("db_path", nargs="?", default=None)
    ap.add_argument("--report-only", action="store_true",
                    help="count and name what would change; write nothing")
    args = ap.parse_args()
    result = run_migration(args.db_path, report_only=args.report_only)
    for table, s in result["per_table"].items():
        if s.get("absent"):
            print("  %-20s table absent on this install" % table)
            continue
        print("  %-20s split=%d  %s=%d  skipped=%d"
              % (table, s["split"],
                 "would heal" if args.report_only else "healed",
                 s["healed"], s["skipped"]))
    for s in result["skipped"]:
        print("    SKIPPED %s %s:%s — %s"
              % (s["table"], s["voucher_type"], str(s["voucher_id"])[:8], s["reason"]))
    print("Migration 037 " + ("report complete (no writes)."
                              if args.report_only else "complete."))
