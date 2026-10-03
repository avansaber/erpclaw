"""Migration 050: register the expense_claim voucher type for payment_allocation.

Paying an approved expense claim allocates a submitted payment to the claim
(erpclaw-payments submit-payment / allocate-payment), and allocation voucher
types are gated against `voucher_type_registry`
(target_table='payment_allocation'). The type was seeded for `gl_entry` only,
so every payment allocated to a claim failed the registry gate.

This seeds the one row that init_schema.VOUCHER_TYPE_REGISTRY_SEED now
carries for fresh installs, so existing DBs match:

  - ('expense_claim', 'erpclaw-hr', 'Expense Claim', 'payment_allocation')
        required: the claim payment goes through the allocation registry
        gate, with the claim id as the voucher id.

Data-seed only — no table/column DDL. Idempotent: the row is inserted only
when the (voucher_type, target_table) pair is absent. A row an operator
deactivated stays deactivated. Forward-only; nothing to roll back but the
one row.
"""
import argparse
import importlib.util
import os
import sys

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

# M102: this is one fixed catalog row, identical on every install. It changes
# no value the install held before the run, so it writes no audit trail.
MIGRATION_DATA_CLASS = "none"

VOUCHER_TYPE = "expense_claim"

# (voucher_type, skill_name, label, target_table)
_SEED = [
    ("expense_claim", "erpclaw-hr", "Expense Claim", "payment_allocation"),
]

_SELECT_PAIR = ("SELECT 1 FROM voucher_type_registry "
                "WHERE voucher_type = ? AND target_table = ?")
_INSERT_ROW = ("INSERT INTO voucher_type_registry "
               "(voucher_type, skill_name, label, target_table) "
               "VALUES (?, ?, ?, ?)")


def run_migration(db_path=None):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    if not seam.table_exists("voucher_type_registry", path):
        print("  voucher_type_registry absent (pre-registry install). Nothing to do.")
        return {"seeded": [], "already": []}
    conn = get_connection(path)
    try:
        seeded, already = [], []
        for vt, skill, label, target in _SEED:
            if conn.execute(_SELECT_PAIR, (vt, target)).fetchone():
                already.append(vt)
            else:
                conn.execute(_INSERT_ROW, (vt, skill, label, target))
                seeded.append(vt)
        conn.commit()
        print(f"  expense_claim voucher type: "
              f"{len(seeded)} seeded, {len(already)} already present.")
        return {"seeded": seeded, "already": already}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 050: register expense_claim voucher type")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 050 complete.")
