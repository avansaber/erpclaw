"""Migration 030: billing_run + billing_run_target (Wave F S1.3, F-debt.3).

Creates the crash-safe batch-orchestration registry that unifies the three
cron-like actions (generate-recurring-invoices, process-recurring, run-billing):
one `billing_run` row per invocation, one `billing_run_target` row per per-item
attempt. Each target processes in its own transaction (document insert + GL +
PLE + cursor advance + target status all-or-nothing), so a crashed run resumes
with zero duplicate documents. Rows are written ONLY through
`erpclaw_lib.billing_run`.

CHECK-constraint justification (ADR-0031 scar-tissue rule): the status /
run_type / target_type enums are literal CHECKs because they are an internal
orchestration state machine consumed exclusively by erpclaw_lib.billing_run —
not an extensible voucher-class domain — and `run_type` already carries the
F-depth 'combined' value, so the planned subscription work needs no ALTER.

Pure CREATE TABLE / CREATE INDEX IF NOT EXISTS — idempotent, dialect-aware
(the DDL text is Postgres-portable as written; migration 018 precedent), no
rebuild, no FK rewrite, no data seed. Columns match init_schema exactly.
"""
import argparse
import os
import sqlite3

DEFAULT_DB_PATH = os.path.join(os.path.expanduser(os.environ.get("ERPCLAW_HOME", "~/.openclaw/erpclaw")), "data.sqlite")

_DDL = [
    """CREATE TABLE IF NOT EXISTS billing_run (
        id              TEXT PRIMARY KEY,
        run_type        TEXT NOT NULL CHECK(run_type IN (
                            'recurring_invoices','recurring_journals',
                            'usage_billing','combined'
                        )),
        company_id      TEXT REFERENCES company(id) ON DELETE RESTRICT,
        as_of_date      TEXT NOT NULL,
        params_json     TEXT,
        status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','running','completed',
                                         'failed','partially_completed')),
        started_at      TEXT,
        finished_at     TEXT,
        total_targets   INTEGER NOT NULL DEFAULT 0,
        targets_processed INTEGER NOT NULL DEFAULT 0,
        targets_succeeded INTEGER NOT NULL DEFAULT 0,
        targets_failed  INTEGER NOT NULL DEFAULT 0,
        error_summary_json TEXT,
        created_by_user_id TEXT,
        created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT DEFAULT CURRENT_TIMESTAMP
    )""",
    "CREATE INDEX IF NOT EXISTS idx_billing_run_status ON billing_run(status)",
    "CREATE INDEX IF NOT EXISTS idx_billing_run_type ON billing_run(run_type)",
    """CREATE TABLE IF NOT EXISTS billing_run_target (
        id              TEXT PRIMARY KEY,
        billing_run_id  TEXT NOT NULL REFERENCES billing_run(id) ON DELETE CASCADE,
        target_type     TEXT NOT NULL CHECK(target_type IN (
                            'recurring_invoice_template',
                            'recurring_journal_template','meter'
                        )),
        target_id       TEXT NOT NULL,
        attempt_at      TEXT,
        status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','processing','done',
                                         'failed','skipped')),
        result_voucher_id TEXT,
        error_message   TEXT,
        created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at      TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(billing_run_id, target_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_brt_run ON billing_run_target(billing_run_id)",
    "CREATE INDEX IF NOT EXISTS idx_brt_run_status ON billing_run_target(billing_run_id, status)",
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
    existed = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='billing_run'"
    ).fetchone() is not None
    for stmt in _DDL:
        conn.execute(stmt)
    conn.commit()
    conn.close()
    print(f"  billing_run/billing_run_target: "
          f"{'already present' if existed else 'created'} (+ indexes).")


def _run_postgres(url):
    import psycopg2
    conn = psycopg2.connect(url)
    try:
        with conn.cursor() as cur:
            for stmt in _DDL:
                cur.execute(stmt)
        conn.commit()
        print("  Postgres: billing_run/billing_run_target ensured (+ indexes).")
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
    parser = argparse.ArgumentParser(description="Migration 030: billing_run orchestration tables")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    run_migration(args.db_path)
    print("Migration 030 complete.")
