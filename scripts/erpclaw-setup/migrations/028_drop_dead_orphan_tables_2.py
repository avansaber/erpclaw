"""Migration 028: Drop the second batch of confirmed-dead orphan tables (M31 H2 / audit B7).

Three foundation tables were defined in init_schema but had ZERO writers and ZERO
readers anywhere in the tree (register-verified: writers=[], readers=[]). They are
dead surface, not empty-feature scaffolding with a live reader. Removed from
init_schema for fresh installs; this drops them from existing DBs so fresh ==
migrated. Sibling drop-batch to migration 013 (audit P2) and integrations
migration 001.

Dropped (each with the evidence for why it is dead):

  communication
      Migration 013 deferred this one as "name collides with the English word;
      ambiguous". The M31 register resolves the ambiguity: zero code references to
      the TABLE (the grep hits are the unrelated hyphenated action names
      `legal-add-communication`, `integration-communication-delivery-report`, a
      k12 goal-area enum value, and legalclaw's own `legalclaw_communication`
      table). The customer-communication story is carried by crm_activity +
      email_log. Also removes the permanently-empty growth UI.yaml `communication`
      entity (handled in the same M31 H2 change).

  erpclaw_module_validation
      Pre-v4.0.0 OS persistence. Since the v4.0.0 dual-layer split the validate/
      inspect layer is deliberately STATELESS — `erpclaw-os/db_query.py` builds its
      table registry in-memory and never persists a validation run. Zero writers.

  erpclaw_table_ownership
      Same pre-v4.0.0 OS persistence. The ownership map is computed at runtime
      (erpclaw-meta's SKILL_TABLES dict), never read from this table. Zero writers.

Idempotent (DROP IF EXISTS), dialect-aware. Forward-only: these tables are empty
(no code ever wrote them), so there is no data to preserve. ADR-0028 clause 4:
dead-surface-only drops (zero writers) keep rollback-foundation's file-only
rollback safe.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

# No inbound FKs to any of these (SIM-verified), so drop order is immaterial.
_DROP_ORDER = [
    "communication",
    "erpclaw_module_validation",
    "erpclaw_table_ownership",
]


def _get_dialect():
    return os.environ.get("ERPCLAW_DB_DIALECT", "sqlite")


def _run_sqlite(path):
    conn = sqlite3.connect(path)
    try:
        from erpclaw_lib.db import setup_pragmas
        setup_pragmas(conn)
    except ImportError:
        conn.execute("PRAGMA busy_timeout=5000")
    dropped = []
    for t in _DROP_ORDER:
        existed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)).fetchone()
        conn.execute(f"DROP TABLE IF EXISTS {t}")
        if existed:
            dropped.append(t)
    conn.commit()
    conn.close()
    print(f"  dropped: {', '.join(dropped) if dropped else '(none — already absent)'}")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for t in _DROP_ORDER:
                cur.execute(f"DROP TABLE IF EXISTS {t}")
        conn.commit()
        print("  Postgres: dead orphan tables (batch 2) dropped (if present).")
    finally:
        conn.close()


def run_migration(db_path=None):
    if _get_dialect() == "postgresql":
        url = os.environ.get("ERPCLAW_DB_URL") or db_path
        if not url:
            print("Postgres dialect set but no connection URL (ERPCLAW_DB_URL). Nothing to migrate.")
            return
        _run_postgres(url)
        return
    path = db_path or os.environ.get("ERPCLAW_DB_PATH", DEFAULT_DB_PATH)
    if not os.path.exists(path):
        print(f"Database not found at {path}. Nothing to migrate.")
        return
    _run_sqlite(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migration 028: drop dead orphan tables (batch 2)")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 028 complete.")
