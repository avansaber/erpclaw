"""Migration 042: Register the two FoodClaw voucher types for gl_entry.

`complete-catering-event` and `add-royalty-entry` post GL through
`gl_posting.insert_gl_entries`, which enforces `voucher_type` validity against
`voucher_type_registry` (target_table='gl_entry'). Neither FoodClaw type was
seeded, so every configured posting failed the gate. Fresh installs now carry
the rows in init_schema.VOUCHER_TYPE_REGISTRY_SEED; this migration brings
existing databases to the same shape.

Data-seed only — no table/column DDL. Idempotent: a row that is already
present is left exactly as it is, including one an operator deactivated.
Forward-only; nothing to roll back but the two rows.
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

# Fixed catalog rows, identical on every install: nothing held before changes.
MIGRATION_DATA_CLASS = "none"

# (voucher_type, skill_name, label, target_table)
_SEED = [
    ("food_catering_revenue", "foodclaw", "Catering Revenue", "gl_entry"),
    ("food_franchise_royalty", "foodclaw", "Franchise Royalty", "gl_entry"),
]

# Fixed statements. Nothing is interpolated into any of them.
_SELECT_ROW = ("SELECT 1 FROM voucher_type_registry "
               "WHERE voucher_type = ? AND target_table = ?")
_INSERT_ROW = ("INSERT INTO voucher_type_registry "
               "(voucher_type, skill_name, label, target_table) "
               "VALUES (?, ?, ?, ?)")


def run_migration(db_path=None):
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    conn = get_connection(path)
    try:
        if not seam.table_exists("voucher_type_registry", path):
            print("  voucher_type_registry absent (pre-M0 install). Nothing to do.")
            return {"seeded": [], "already": []}
        seeded = []
        already = []
        for voucher_type, skill_name, label, target_table in _SEED:
            row = conn.execute(_SELECT_ROW, (voucher_type, target_table)).fetchone()
            if row is None:
                conn.execute(_INSERT_ROW,
                             (voucher_type, skill_name, label, target_table))
                seeded.append(voucher_type)
            else:
                already.append(voucher_type)
        conn.commit()
        print("  foodclaw voucher types: %d seeded, %d already present."
              % (len(seeded), len(already)))
        return {"seeded": seeded, "already": already}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Migration 042: register FoodClaw voucher types")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 042 complete.")
